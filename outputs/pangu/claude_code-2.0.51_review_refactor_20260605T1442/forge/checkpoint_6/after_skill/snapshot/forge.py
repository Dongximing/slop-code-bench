#!/usr/bin/env python3
"""Forge CLI tool for managing blueprints."""

import argparse
import json
import sys
import uuid as uuid_module
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


class ForgeError(Exception):
    """Base exception for Forge errors."""

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)

    def to_json(self) -> str:
        return json.dumps({"error": self.error, "detail": self.detail})


class ValidationError(ForgeError):
    """Raised when input fails validation."""
    error = "validation_error"


class NotFoundError(ForgeError):
    """Raised when a resource was not found."""
    error = "not_found"


class InvalidOperationError(ForgeError):
    """Raised when an operation is invalid."""
    error = "invalid_operation"


class DuplicateAssignmentError(ForgeError):
    """Raised when a duplicate assignment is detected."""
    error = "duplicate_assignment"


class ConflictError(ForgeError):
    """Raised when a resource already exists."""
    error = "conflict"


class InvalidTransitionError(ForgeError):
    """Raised when an action does not apply to the current status."""
    error = "invalid_transition"


class InvalidImageError(ForgeError):
    """Raised when an image UUID fails format validation."""
    error = "invalid_image"


class FlashFailedError(ForgeError):
    """Raised when flashing firmware with a well-formed but unknown image UUID."""
    error = "flash_failed"


class UnknownCommandError(ForgeError):
    """Raised when an unknown command is invoked."""
    error = "unknown_command"


@dataclass
class RequirementSet:
    """A requirement set for a blueprint."""
    resource_type: str
    resource_count: int
    capabilities: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        result = {
            "resource_type": self.resource_type,
            "resource_count": self.resource_count
        }
        if self.capabilities:
            result["capabilities"] = self.capabilities
        return result


@dataclass
class Blueprint:
    """A blueprint resource."""
    uuid: str
    name: str
    requirement_sets: list[RequirementSet]
    created_at: str

    def to_dict(self) -> dict:
        return {
            "uuid": self.uuid,
            "name": self.name,
            "requirement_sets": [rs.to_dict() for rs in self.requirement_sets],
            "created_at": self.created_at
        }


@dataclass
class Allocation:
    """An allocation resource derived from a blueprint."""
    uuid: str
    blueprint_name: str
    requirement_set_index: int
    binding_status: str
    assignment_id: Optional[str]
    created_at: str

    def to_dict(self) -> dict:
        return {
            "uuid": self.uuid,
            "blueprint_name": self.blueprint_name,
            "requirement_set_index": self.requirement_set_index,
            "binding_status": self.binding_status,
            "assignment_id": self.assignment_id,
            "created_at": self.created_at
        }


@dataclass
class Unit:
    """A unit resource."""
    uuid: str
    name: str
    capacity: int
    status: str  # "active" or "inactive"
    capabilities: list[str] = field(default_factory=list)
    created_at: str = ""
    module_uuids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        result = {
            "uuid": self.uuid,
            "name": self.name,
            "capacity": self.capacity,
            "status": self.status,
            "created_at": self.created_at
        }
        # Always include capabilities (sorted ascending lexicographic)
        result["capabilities"] = sorted(self.capabilities)
        # reserved_capacity is 0 for active, capacity for inactive
        result["reserved_capacity"] = 0 if self.status == "active" else self.capacity
        # Include module_uuids sorted ascending lexicographic
        result["module_uuids"] = sorted(self.module_uuids)
        return result


@dataclass
class Module:
    """A module resource associated with a unit."""
    uuid: str
    unit_uuid: str
    component_type: str
    firmware_image: Optional[str]
    created_at: str

    def to_dict(self) -> dict:
        return {
            "uuid": self.uuid,
            "unit_uuid": self.unit_uuid,
            "component_type": self.component_type,
            "firmware_image": self.firmware_image,
            "created_at": self.created_at
        }


@dataclass
class Tag:
    """A tag resource (key-value metadata) attached to a module."""
    uuid: str
    module_uuid: str
    key: str
    value: str
    created_at: str

    def to_dict(self) -> dict:
        return {
            "uuid": self.uuid,
            "module_uuid": self.module_uuid,
            "key": self.key,
            "value": self.value,
            "created_at": self.created_at
        }


class ValidationErrorCollector:
    """Collects multiple validation errors and raises them together."""

    def __init__(self):
        self.errors: list[str] = []

    def add(self, message: str):
        self.errors.append(message)

    def raise_if_any(self):
        if self.errors:
            raise ValidationError("; ".join(self.errors))


class BlueprintStore:
    """Manages blueprint CRUD with JSON persistence."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.file_path = self.data_dir / "blueprints.json"
        self._blueprints: dict[str, Blueprint] = {}
        self._name_to_uuid: dict[str, str] = {}
        self._load()

    def _load(self):
        """Load blueprints from disk. On corruption, starts fresh."""
        if self.file_path.exists():
            try:
                with open(self.file_path, 'r') as f:
                    data = json.load(f)
                    for bp_data in data:
                        requirement_sets = [
                            RequirementSet(
                                resource_type=rs["resource_type"],
                                resource_count=rs["resource_count"],
                                capabilities=rs.get("capabilities", [])
                            )
                            for rs in bp_data["requirement_sets"]
                        ]
                        blueprint = Blueprint(
                            uuid=bp_data["uuid"],
                            name=bp_data["name"],
                            requirement_sets=requirement_sets,
                            created_at=bp_data["created_at"]
                        )
                        self._blueprints[blueprint.uuid] = blueprint
                        self._name_to_uuid[blueprint.name] = blueprint.uuid
            except (json.JSONDecodeError, KeyError, TypeError):
                # If file is corrupted, start fresh
                self._blueprints = {}
                self._name_to_uuid = {}

    def _save(self):
        """Save to disk."""
        data = [bp.to_dict() for bp in self._blueprints.values()]
        with open(self.file_path, 'w') as f:
            json.dump(data, f, indent=2)

    def create(self, name: str, requirement_sets: list[RequirementSet]) -> Blueprint:
        """Create a new blueprint."""
        if name in self._name_to_uuid:
            raise ConflictError(f"Blueprint with name '{name}' already exists")

        blueprint_uuid = str(uuid_module.uuid4()).lower()
        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        blueprint = Blueprint(
            uuid=blueprint_uuid,
            name=name,
            requirement_sets=requirement_sets,
            created_at=created_at
        )

        self._blueprints[blueprint_uuid] = blueprint
        self._name_to_uuid[name] = blueprint_uuid
        self._save()

        return blueprint

    def list_all(self) -> list[Blueprint]:
        """List all blueprints sorted by created_at, then name, then uuid."""
        blueprints = list(self._blueprints.values())
        blueprints.sort(key=lambda bp: (bp.created_at, bp.name, bp.uuid))
        return blueprints

    def get_by_uuid(self, uuid: str) -> Optional[Blueprint]:
        """Get a blueprint by UUID."""
        return self._blueprints.get(uuid)

    def get_by_name(self, name: str) -> Optional[Blueprint]:
        """Get a blueprint by name."""
        uuid = self._name_to_uuid.get(name)
        if uuid:
            return self._blueprints.get(uuid)
        return None

    def delete_by_uuid(self, uuid: str) -> bool:
        """Delete a blueprint by UUID. Returns True if deleted."""
        if uuid not in self._blueprints:
            return False

        blueprint = self._blueprints[uuid]
        del self._blueprints[uuid]
        del self._name_to_uuid[blueprint.name]
        self._save()
        return True

    def delete_by_names(self, names: list[str]) -> tuple[int, list[str]]:
        """
        Delete blueprints by names.
        Returns (count, unresolved_names).
        """
        unresolved = []
        for name in names:
            if name not in self._name_to_uuid:
                unresolved.append(name)

        if unresolved:
            return 0, unresolved

        # All names exist, perform deletion
        count = 0
        for name in names:
            uuid = self._name_to_uuid[name]
            blueprint = self._blueprints[uuid]
            del self._blueprints[uuid]
            del self._name_to_uuid[name]
            count += 1

        self._save()
        return count, []


class AllocationStore:
    """Persistent storage for allocations."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.file_path = self.data_dir / "allocations.json"
        self._allocations: dict[str, Allocation] = {}
        self._load()

    def _load(self):
        """Load allocations from disk."""
        if self.file_path.exists():
            try:
                with open(self.file_path, 'r') as f:
                    data = json.load(f)
                    for alloc_data in data:
                        allocation = Allocation(
                            uuid=alloc_data["uuid"],
                            blueprint_name=alloc_data["blueprint_name"],
                            requirement_set_index=alloc_data["requirement_set_index"],
                            binding_status=alloc_data["binding_status"],
                            assignment_id=alloc_data.get("assignment_id"),
                            created_at=alloc_data["created_at"]
                        )
                        self._allocations[allocation.uuid] = allocation
            except (json.JSONDecodeError, KeyError, TypeError):
                # If file is corrupted, start fresh
                self._allocations = {}

    def _save(self):
        """Save allocations to disk."""
        data = [alloc.to_dict() for alloc in self._allocations.values()]
        with open(self.file_path, 'w') as f:
            json.dump(data, f, indent=2)

    def bind_allocation(self, uuid: str, assignment_id: str) -> bool:
        """
        Bind an allocation to an assignment.
        Returns True if successful, False if allocation doesn't exist.
        """
        if uuid not in self._allocations:
            return False
        allocation = self._allocations[uuid]
        allocation.binding_status = "bound"
        allocation.assignment_id = assignment_id
        self._save()
        return True

    def unbind_allocation(self, uuid: str) -> bool:
        """
        Unbind an allocation.
        Returns True if successful, False if allocation doesn't exist.
        """
        if uuid not in self._allocations:
            return False
        allocation = self._allocations[uuid]
        allocation.binding_status = "unbound"
        allocation.assignment_id = None
        self._save()
        return True

    def get_allocations_by_assignment(self, assignment_id: str) -> list[Allocation]:
        """Get all allocations with a specific assignment_id."""
        return [a for a in self._allocations.values() if a.assignment_id == assignment_id]

    def get_bound_allocations_by_assignment(self, assignment_id: str) -> list[Allocation]:
        """Get all bound allocations with a specific assignment_id."""
        return [
            a for a in self._allocations.values()
            if a.assignment_id == assignment_id and a.binding_status == "bound"
        ]

    def delete_by_assignment(self, assignment_id: str) -> int:
        """
        Delete all allocations with the given assignment_id.
        Returns the number of allocations deleted.
        """
        to_delete = [uuid for uuid, alloc in self._allocations.items() if alloc.assignment_id == assignment_id]
        for uuid in to_delete:
            del self._allocations[uuid]
        count = len(to_delete)
        if count > 0:
            self._save()
        return count

    def delete_by_ids(self, uuids: list[str]) -> tuple[int, list[str]]:
        """
        Delete allocations by UUID list.
        Returns (count_deleted, unresolved_uuids).
        All UUIDs must exist or nothing is deleted.
        """
        # Check all exist first
        unresolved = [uuid for uuid in uuids if uuid not in self._allocations]
        if unresolved:
            return 0, unresolved

        # Delete all
        for uuid in uuids:
            del self._allocations[uuid]
        self._save()
        return len(uuids), []

    def create_from_blueprint(self, blueprint: Blueprint, created_at: str) -> list[Allocation]:
        """
        Create allocations from a blueprint's requirement sets.
        Returns list of created allocations.
        """
        allocations = []
        for index, rs in enumerate(blueprint.requirement_sets):
            # Check if this requirement set should produce 2 allocations
            double = any(cap == "category=TYPE_A" for cap in rs.capabilities)

            for _ in range(2 if double else 1):
                alloc = Allocation(
                    uuid=str(uuid_module.uuid4()).lower(),
                    blueprint_name=blueprint.name,
                    requirement_set_index=index,
                    binding_status="unbound",
                    assignment_id=None,
                    created_at=created_at
                )
                self._allocations[alloc.uuid] = alloc
                allocations.append(alloc)

        self._save()
        return allocations


class UnitStore:
    """Persistent storage for units with capability ledger."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.file_path = self.data_dir / "units.json"
        self._units: dict[str, Unit] = {}
        self._name_to_uuid: dict[str, str] = {}
        self._load()

    def _load(self):
        """Load units from disk. On corruption, starts fresh."""
        if self.file_path.exists():
            try:
                with open(self.file_path, 'r') as f:
                    data = json.load(f)
                    for unit_data in data:
                        unit = Unit(
                            uuid=unit_data["uuid"],
                            name=unit_data["name"],
                            capacity=unit_data["capacity"],
                            status=unit_data["status"],
                            capabilities=unit_data.get("capabilities", []),
                            created_at=unit_data.get("created_at", "")
                        )
                        self._units[unit.uuid] = unit
                        self._name_to_uuid[unit.name] = unit.uuid
            except (json.JSONDecodeError, KeyError, TypeError):
                # If file is corrupted, start fresh
                self._units = {}
                self._name_to_uuid = {}

    def _save(self):
        """Save units to disk."""
        data = [u.to_dict() for u in self._units.values()]
        with open(self.file_path, 'w') as f:
            json.dump(data, f, indent=2)

    def get_capability_ledger(self) -> list[str]:
        """
        Get the set of capabilities from all active units.
        Returns sorted unique list of capabilities.
        """
        caps = set()
        for unit in self._units.values():
            if unit.status == "active":
                caps.update(unit.capabilities)
        return sorted(caps)

    def create(self, name: str, capacity: int, capabilities: list[str], created_at: str) -> Unit:
        """Create a new unit. Returns the created unit."""
        if name in self._name_to_uuid:
            raise ConflictError(f"Unit with name '{name}' already exists")

        unit_uuid = str(uuid_module.uuid4()).lower()
        unit = Unit(
            uuid=unit_uuid,
            name=name,
            capacity=capacity,
            status="inactive",
            capabilities=capabilities,
            created_at=created_at
        )

        self._units[unit_uuid] = unit
        self._name_to_uuid[name] = unit_uuid
        self._save()

        return unit

    def activate(self, uuid: str) -> Unit:
        """
        Activate a unit, transitioning from inactive to active.
        Returns the updated unit.
        """
        unit = self._units.get(uuid)
        if unit is None:
            raise NotFoundError(f"No unit found with uuid '{uuid}'")
        if unit.status == "active":
            raise InvalidTransitionError(f"Unit '{uuid}' is already active")

        unit.status = "active"
        self._save()

        return unit

    def deactivate(self, uuid: str) -> Unit:
        """
        Deactivate a unit, transitioning from active to inactive.
        Returns the updated unit.
        """
        unit = self._units.get(uuid)
        if unit is None:
            raise NotFoundError(f"No unit found with uuid '{uuid}'")
        if unit.status == "inactive":
            raise InvalidTransitionError(f"Unit '{uuid}' is already inactive")

        unit.status = "inactive"
        self._save()

        return unit

    def get_by_uuid(self, uuid: str) -> Optional[Unit]:
        """Get a unit by UUID."""
        return self._units.get(uuid)

    def list_all(self) -> list[Unit]:
        """
        List all units sorted by created_at (ascending), then name, then uuid.
        """
        units = list(self._units.values())
        units.sort(key=lambda u: (u.created_at, u.name, u.uuid))
        return units



class ModuleStore:
    """Persistent storage for modules with firmware flashing capability."""

    def __init__(self, data_dir: Path, unit_store: UnitStore):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.file_path = self.data_dir / "modules.json"
        self._unit_store = unit_store
        self._modules: dict[str, Module] = {}
        self._load()

    def _load(self):
        """Load modules from disk. On corruption, starts fresh."""
        if self.file_path.exists():
            try:
                with open(self.file_path, 'r') as f:
                    data = json.load(f)
                    for mod_data in data:
                        module = Module(
                            uuid=mod_data["uuid"],
                            unit_uuid=mod_data["unit_uuid"],
                            component_type=mod_data["component_type"],
                            firmware_image=mod_data.get("firmware_image"),
                            created_at=mod_data["created_at"]
                        )
                        self._modules[module.uuid] = module
            except (json.JSONDecodeError, KeyError, TypeError):
                # If file is corrupted, start fresh
                self._modules = {}

    def _save(self):
        """Save modules to disk."""
        data = [m.to_dict() for m in self._modules.values()]
        with open(self.file_path, 'w') as f:
            json.dump(data, f, indent=2)

    def _get_known_images(self) -> set[str]:
        """Get set of all known firmware image UUIDs (non-null firmware_image values)."""
        images = set()
        for module in self._modules.values():
            if module.firmware_image is not None:
                images.add(module.firmware_image)
        return images

    def _is_valid_uuid_format(self, value: str) -> bool:
        """Check if value matches lowercase-hex UUID format (8-4-4-4-12)."""
        if not isinstance(value, str):
            return False
        import re
        pattern = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
        return bool(pattern.match(value))

    def create(self, unit_uuid: str, component_type: str, firmware_image: Optional[str] = None) -> Module:
        """Create a new module."""
        # Validate unit_uuid exists
        if self._unit_store.get_by_uuid(unit_uuid) is None:
            raise NotFoundError(f"Unit '{unit_uuid}' not found")

        # Validate component_type
        if not component_type or not isinstance(component_type, str):
            raise ValidationError("component_type is required and must be non-empty")

        # Validate firmware_image format if provided
        if firmware_image is not None:
            if not self._is_valid_uuid_format(firmware_image):
                raise InvalidImageError("firmware_image must be a lowercase UUID")

        module_uuid = str(uuid_module.uuid4()).lower()
        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        module = Module(
            uuid=module_uuid,
            unit_uuid=unit_uuid,
            component_type=component_type,
            firmware_image=firmware_image,
            created_at=created_at
        )

        self._modules[module_uuid] = module

        # Add module reference to parent unit
        unit = self._unit_store.get_by_uuid(unit_uuid)
        if unit:
            unit.module_uuids.append(module_uuid)
            # Note: module_uuids should be sorted when accessed via to_dict()
            self._unit_store._save()

        self._save()
        return module

    def list_all(self, unit_filter: Optional[str] = None, filters: Optional[list[tuple[str, str]]] = None) -> list[Module]:
        """
        List all modules with optional filters.
        Filters: (field_name, value) tuples.
        Results ordered by unit_uuid ascending, then created_at ascending, then uuid ascending.
        """
        modules = list(self._modules.values())

        # Apply unit filter
        if unit_filter is not None:
            modules = [m for m in modules if m.unit_uuid == unit_filter]

        # Apply generic filters
        if filters:
            for field_name, value in filters:
                if field_name == "unit_uuid":
                    modules = [m for m in modules if m.unit_uuid == value]
                elif field_name == "component_type":
                    modules = [m for m in modules if m.component_type == value]
                elif field_name == "firmware_image":
                    if value == "null":
                        modules = [m for m in modules if m.firmware_image is None]
                    else:
                        modules = [m for m in modules if m.firmware_image == value]
                # Unknown fields match no rows (not an error)
                elif field_name not in ("uuid", "created_at"):
                    modules = []

        # Sort by unit_uuid ascending, then created_at ascending, then uuid ascending
        modules.sort(key=lambda m: (m.unit_uuid, m.created_at, m.uuid))
        return modules

    def get_by_uuid(self, uuid: str) -> Optional[Module]:
        """Get a module by UUID."""
        return self._modules.get(uuid)

    def flash(self, uuid: str, image_id: str) -> Module:
        """
        Flash firmware to a module.
        Returns the updated module.
        """
        # 1. Format validation
        if not self._is_valid_uuid_format(image_id):
            raise InvalidImageError("firmware_image must be a lowercase UUID")

        # 2. Module existence
        module = self._modules.get(uuid)
        if module is None:
            raise NotFoundError(f"Module '{uuid}' not found")

        # 3. Image lookup
        known_images = self._get_known_images()
        if image_id not in known_images:
            raise FlashFailedError("flash failed: image not found")

        # Flash the firmware
        module.firmware_image = image_id
        self._save()
        return module


class TagStore:
    """Persistent storage for tags (key-value metadata on modules)."""

    def __init__(self, data_dir: Path, module_store: ModuleStore):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.file_path = self.data_dir / "tags.json"
        self._module_store = module_store
        self._tags: dict[str, Tag] = {}
        self._key_to_uuid: dict[str, str] = {}  # (module_uuid, key) -> tag_uuid
        self._load()

    def _load(self):
        """Load tags from disk. On corruption, starts fresh."""
        if self.file_path.exists():
            try:
                with open(self.file_path, 'r') as f:
                    data = json.load(f)
                    for tag_data in data:
                        tag = Tag(
                            uuid=tag_data["uuid"],
                            module_uuid=tag_data["module_uuid"],
                            key=tag_data["key"],
                            value=tag_data["value"],
                            created_at=tag_data["created_at"]
                        )
                        self._tags[tag.uuid] = tag
                        self._key_to_uuid[(tag.module_uuid, tag.key)] = tag.uuid
            except (json.JSONDecodeError, KeyError, TypeError):
                # If file is corrupted, start fresh
                self._tags = {}
                self._key_to_uuid = {}

    def _save(self):
        """Save tags to disk."""
        data = [t.to_dict() for t in self._tags.values()]
        with open(self.file_path, 'w') as f:
            json.dump(data, f, indent=2)

    def create(self, module_uuid: str, key: str, value: str) -> Tag:
        """Create a new tag."""
        # Validate module exists
        if self._module_store.get_by_uuid(module_uuid) is None:
            raise NotFoundError(f"Module '{module_uuid}' not found")

        # Validate required fields
        if not key or not isinstance(key, str):
            raise ValidationError("key is required and must be non-empty")
        if not value or not isinstance(value, str):
            raise ValidationError("value is required and must be non-empty")

        # Check for duplicate (module_uuid, key) pair
        if (module_uuid, key) in self._key_to_uuid:
            raise ConflictError(f"Tag with key '{key}' already exists for module '{module_uuid}'")

        tag_uuid = str(uuid_module.uuid4()).lower()
        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        tag = Tag(
            uuid=tag_uuid,
            module_uuid=module_uuid,
            key=key,
            value=value,
            created_at=created_at
        )

        self._tags[tag_uuid] = tag
        self._key_to_uuid[(module_uuid, key)] = tag_uuid
        self._save()
        return tag

    def list_all(self, module_filter: Optional[str] = None, key_filter: Optional[str] = None) -> list[Tag]:
        """
        List all tags with optional filters.
        Results ordered by module_uuid ascending, then key ascending, then uuid ascending.
        """
        tags = list(self._tags.values())

        # Apply filters
        if module_filter is not None:
            tags = [t for t in tags if t.module_uuid == module_filter]
        if key_filter is not None:
            tags = [t for t in tags if t.key == key_filter]

        # Sort by module_uuid ascending, then key ascending, then uuid ascending
        tags.sort(key=lambda t: (t.module_uuid, t.key, t.uuid))
        return tags

    def get_by_uuid(self, uuid: str) -> Optional[Tag]:
        """Get a tag by UUID."""
        return self._tags.get(uuid)

    def delete_by_uuid(self, uuid: str) -> Tag:
        """Delete a tag by UUID. Returns the deleted tag."""
        if uuid not in self._tags:
            raise NotFoundError(f"Tag '{uuid}' not found")

        tag = self._tags[uuid]
        del self._key_to_uuid[(tag.module_uuid, tag.key)]
        del self._tags[uuid]
        self._save()
        return tag


def validate_unit_create_payload(data: Any) -> tuple[str, int, list[str]]:
    """Validate unit create payload. Returns (name, capacity, capabilities)."""
    collector = ValidationErrorCollector()

    # Payload must be an object
    if not isinstance(data, dict):
        collector.add("payload must be an object")
        collector.raise_if_any()

    # name is required
    if "name" not in data:
        collector.add("name is required")
    elif not isinstance(data["name"], str):
        collector.add("name must be a string")
    elif len(data["name"]) == 0:
        collector.add("name must be non-empty")

    # capacity is required
    if "capacity" not in data:
        collector.add("capacity is required")
    else:
        # Boolean values are not integers
        if isinstance(data["capacity"], bool):
            collector.add("capacity must be an integer, not boolean")
        elif not isinstance(data["capacity"], int):
            collector.add("capacity must be an integer")
        elif data["capacity"] < 1:
            collector.add("capacity must be positive (>= 1)")

    # capabilities is optional
    capabilities: list[str] = []
    if "capabilities" in data:
        caps = data["capabilities"]
        if not isinstance(caps, list):
            collector.add("capabilities must be an array of strings")
        else:
            for i, cap in enumerate(caps):
                if not isinstance(cap, str):
                    collector.add(f"capabilities[{i}] must be a string")
            capabilities = [c for c in caps if isinstance(c, str)]

    collector.raise_if_any()
    name = data["name"]
    capacity = data["capacity"]

    # Ensure capacity is an integer (should already be validated)
    if isinstance(capacity, bool):
        capacity = 0 if not capacity else 1
    elif not isinstance(capacity, int):
        capacity = int(capacity) if str(capacity).isdigit() else 0

    return name, capacity, capabilities


def cmd_unit_create(store: UnitStore, stdin_data: str):
    """Handle unit create command."""
    try:
        data = json.loads(stdin_data)
    except json.JSONDecodeError:
        output_error(ValidationError("stdin is not valid JSON"))

    try:
        name, capacity, capabilities = validate_unit_create_payload(data)
        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        unit = store.create(name, capacity, capabilities, created_at)
        output_json(unit.to_dict())
    except ForgeError as e:
        output_error(e)


def cmd_unit_list(store: UnitStore):
    """Handle unit list command."""
    units = store.list_all()
    output_json([u.to_dict() for u in units])


def cmd_unit_get(store: UnitStore, uuid: str):
    """Handle unit get command."""
    unit = store.get_by_uuid(uuid)
    if unit is None:
        output_error(NotFoundError(f"No unit found with uuid '{uuid}'"))
    output_json(unit.to_dict())


def cmd_unit_activate(store: UnitStore, uuid: str):
    """Handle unit activate command."""
    try:
        unit = store.activate(uuid)
        output_json(unit.to_dict())
    except ForgeError as e:
        output_error(e)


def cmd_unit_deactivate(store: UnitStore, uuid: str):
    """Handle unit deactivate command."""
    try:
        unit = store.deactivate(uuid)
        output_json(unit.to_dict())
    except ForgeError as e:
        output_error(e)


def cmd_ledger_capabilities(store: UnitStore):
    """Handle ledger capabilities command."""
    caps = store.get_capability_ledger()
    output_json(caps)


def cmd_allocation_list(allocation_store: AllocationStore, assignment_id: Optional[str] = None, status: Optional[str] = None):
    """
    List all allocations, optionally filtered by assignment_id and/or status.
    Results ordered by created_at ascending, then uuid ascending lexicographic.
    """
    allocations = list(allocation_store._allocations.values())

    # Apply filters
    if assignment_id is not None:
        allocations = [a for a in allocations if a.assignment_id == assignment_id]
    if status is not None:
        allocations = [a for a in allocations if a.binding_status == status]

    # Sort by created_at ascending, then uuid ascending lexicographic
    allocations.sort(key=lambda a: (a.created_at, a.uuid))

    return [a.to_dict() for a in allocations]


def validate_requirement_set(rs: Any, collector: ValidationErrorCollector, index: int):
    """Validate a single requirement set."""
    prefix = f"requirement_sets[{index}]"

    if not isinstance(rs, dict):
        collector.add(f"{prefix} must be an object")
        return

    # Check for unsupported keys
    supported_keys = {"resource_type", "resource_count", "capabilities"}
    for key in rs.keys():
        if key not in supported_keys:
            collector.add(f"{prefix}.{key} is an unsupported key")

    # Validate resource_type
    if "resource_type" not in rs:
        collector.add(f"{prefix}.resource_type is required")
    elif not isinstance(rs["resource_type"], str):
        collector.add(f"{prefix}.resource_type must be a string")

    # Validate resource_count
    if "resource_count" not in rs:
        collector.add(f"{prefix}.resource_count is required")
    else:
        # Boolean values are not integers
        if isinstance(rs["resource_count"], bool):
            collector.add(f"{prefix}.resource_count must be an integer, not boolean")
        elif not isinstance(rs["resource_count"], int):
            collector.add(f"{prefix}.resource_count must be an integer")
        elif rs["resource_count"] < 1:
            collector.add(f"{prefix}.resource_count must be positive (>= 1)")

    # Validate capabilities
    if "capabilities" in rs:
        caps = rs["capabilities"]
        if not isinstance(caps, list):
            collector.add(f"{prefix}.capabilities must be an array of strings")
        else:
            for i, cap in enumerate(caps):
                if not isinstance(cap, str):
                    collector.add(f"{prefix}.capabilities[{i}] must be a string")


def parse_requirement_sets(data: list[Any], collector: ValidationErrorCollector) -> list[RequirementSet]:
    """Parse and validate requirement sets from input data."""
    result = []
    for i, rs in enumerate(data):
        validate_requirement_set(rs, collector, i)

        # Only create RequirementSet if validation passed for required fields
        if (isinstance(rs, dict) and
            "resource_type" in rs and isinstance(rs["resource_type"], str) and
            "resource_count" in rs and isinstance(rs["resource_count"], int) and
            rs["resource_count"] >= 1 and
            not any(f"requirement_sets[{i}].resource_type" in err for err in collector.errors) and
            not any(f"requirement_sets[{i}].resource_count" in err for err in collector.errors)):

            caps = []
            if "capabilities" in rs and isinstance(rs["capabilities"], list):
                caps = [c for c in rs["capabilities"] if isinstance(c, str)]

            result.append(RequirementSet(
                resource_type=rs["resource_type"],
                resource_count=rs["resource_count"],
                capabilities=caps
            ))

    return result


def validate_blueprint_input(data: dict) -> tuple[str, list[RequirementSet]]:
    """Validate blueprint creation input. Returns (name, requirement_sets)."""
    collector = ValidationErrorCollector()

    # Validate name
    if "name" not in data:
        collector.add("name is required")
    elif not isinstance(data["name"], str):
        collector.add("name must be a string")
    elif len(data["name"]) == 0:
        collector.add("name must be non-empty")

    # Validate requirement_sets
    if "requirement_sets" not in data:
        collector.add("requirement_sets is required")
    elif not isinstance(data["requirement_sets"], list):
        collector.add("requirement_sets must be an array")
    elif len(data["requirement_sets"]) == 0:
        collector.add("requirement_sets must be non-empty")

    collector.raise_if_any()

    # Parse requirement sets
    requirement_sets = parse_requirement_sets(data["requirement_sets"], collector)
    collector.raise_if_any()

    name = data["name"]
    if not isinstance(name, str):
        # This shouldn't happen due to validation above, but just in case
        name = str(name)

    return name, requirement_sets


def output_json(data: Any):
    """Output JSON and exit successfully."""
    print(json.dumps(data))
    sys.exit(0)


def output_error(error: ForgeError):
    """Output error JSON and exit with failure."""
    print(error.to_json())
    sys.exit(1)


def cmd_create(store: BlueprintStore, stdin_data: str):
    """Handle blueprint create command."""
    try:
        data = json.loads(stdin_data)
    except json.JSONDecodeError:
        output_error(ValidationError("stdin is not valid JSON"))

    try:
        name, requirement_sets = validate_blueprint_input(data)
        blueprint = store.create(name, requirement_sets)
        output_json(blueprint.to_dict())
    except ForgeError as e:
        output_error(e)


def cmd_list(store: BlueprintStore):
    """Handle blueprint list command."""
    blueprints = store.list_all()
    output_json([bp.to_dict() for bp in blueprints])


def cmd_get(store: BlueprintStore, uuid: str):
    """Handle blueprint get command."""
    blueprint = store.get_by_uuid(uuid)
    if blueprint is None:
        output_error(NotFoundError(f"No blueprint found with uuid '{uuid}'"))
    output_json(blueprint.to_dict())


def cmd_delete_uuid(store: BlueprintStore, uuid: str):
    """Handle blueprint delete by UUID command."""
    if not store.delete_by_uuid(uuid):
        output_error(NotFoundError(f"No blueprint found with uuid '{uuid}'"))
    output_json({"deleted": 1})


def cmd_delete_names(store: BlueprintStore, names_str: str):
    """Handle blueprint delete by names command."""
    # Handle empty string - split on comma, but preserve empty elements
    names = names_str.split(",") if names_str else [""]

    count, unresolved = store.delete_by_names(names)
    if unresolved:
        output_error(NotFoundError(f"Unresolved names: {', '.join(unresolved)}"))
    output_json({"deleted": count})


def validate_allocation_create_payload(data: Any) -> str:
    """Validate allocation create payload. Returns blueprint_name."""
    collector = ValidationErrorCollector()

    # Payload must be an object
    if not isinstance(data, dict):
        collector.add("payload must be an object")
        collector.raise_if_any()

    # blueprint_name is required
    if "blueprint_name" not in data:
        collector.add("blueprint_name is required")
    elif not isinstance(data["blueprint_name"], str):
        collector.add("blueprint_name must be a string")
    elif len(data["blueprint_name"]) == 0:
        collector.add("blueprint_name must be non-empty")

    collector.raise_if_any()

    return data["blueprint_name"]


def cmd_allocation_create(blueprint_store: BlueprintStore, allocation_store: AllocationStore, stdin_data: str):
    """Handle allocation create command."""
    try:
        data = json.loads(stdin_data)
    except json.JSONDecodeError:
        output_error(ValidationError("stdin is not valid JSON"))

    try:
        blueprint_name = validate_allocation_create_payload(data)
    except ForgeError as e:
        output_error(e)

    # Lookup blueprint
    blueprint = blueprint_store.get_by_name(blueprint_name)
    if blueprint is None:
        output_error(NotFoundError(f"Blueprint '{blueprint_name}' not found"))

    # Create allocations from blueprint
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    allocations = allocation_store.create_from_blueprint(blueprint, created_at)

    # Output allocations ordered by requirement_set_index ascending
    # (multiplied allocations for same set are already consecutive)
    output_json([a.to_dict() for a in allocations])


def cmd_allocation_list(allocation_store: AllocationStore, assignment_id: Optional[str] = None, status: Optional[str] = None):
    """Handle allocation list command."""
    allocations = allocation_store.list_all(assignment_id=assignment_id, status=status)
    output_json([a.to_dict() for a in allocations])


def cmd_allocation_get(allocation_store: AllocationStore, uuid: str):
    """Handle allocation get command."""
    allocation = allocation_store.get_by_uuid(uuid)
    if allocation is None:
        output_error(NotFoundError(f"No allocation found with uuid '{uuid}'"))
    output_json(allocation.to_dict())


def validate_allocation_bind_payload(data: Any) -> tuple[str, dict[str, str]]:
    """
    Validate allocation bind payload.
    Returns (assignment_id, allocations dict).
    """
    collector = ValidationErrorCollector()

    # Payload must be an object
    if not isinstance(data, dict):
        collector.add("payload must be an object")
        collector.raise_if_any()

    # Check assignment_id is required
    if "assignment_id" not in data:
        collector.add("assignment_id is required")
    elif not isinstance(data["assignment_id"], str):
        collector.add("assignment_id must be a string")
    elif len(data["assignment_id"]) == 0:
        collector.add("assignment_id must be non-empty")

    # Check allocations is required
    if "allocations" not in data:
        collector.add("allocations is required")
    elif not isinstance(data["allocations"], dict):
        collector.add("allocations must be an object")
    elif len(data["allocations"]) == 0:
        collector.add("allocations must not be empty")

    collector.raise_if_any()

    assignment_id = data["assignment_id"]
    allocations = data["allocations"]

    # Validate operations
    for alloc_uuid, operation in allocations.items():
        if operation not in ("add", "remove"):
            raise InvalidOperationError(f"Invalid operation '{operation}' for allocation '{alloc_uuid}', must be 'add' or 'remove'")

    return assignment_id, allocations


def cmd_allocation_bind(allocation_store: AllocationStore, stdin_data: str):
    """Handle allocation bind command."""
    try:
        data = json.loads(stdin_data)
    except json.JSONDecodeError:
        output_error(ValidationError("stdin is not valid JSON"))

    try:
        assignment_id, allocations_ops = validate_allocation_bind_payload(data)
    except ForgeError as e:
        output_error(e)

    # Check all allocation UUIDs exist first
    for alloc_uuid in allocations_ops:
        if allocation_store.get_by_uuid(alloc_uuid) is None:
            output_error(NotFoundError(f"No allocation found with uuid '{alloc_uuid}'"))

    # Check for duplicate assignment guard
    has_add = any(op == "add" for op in allocations_ops.values())
    if has_add:
        existing_bound = allocation_store.get_bound_allocations_by_assignment(assignment_id)
        if len(existing_bound) > 0:
            output_error(DuplicateAssignmentError("Target assignment_id already has bound allocations"))

    # Apply all operations atomically
    for alloc_uuid, operation in allocations_ops.items():
        if operation == "add":
            allocation_store.bind_allocation(alloc_uuid, assignment_id)
        else:  # operation == "remove"
            allocation_store.unbind_allocation(alloc_uuid)

    output_json({"status": "accepted"})


def cmd_allocation_delete_assignment(allocation_store: AllocationStore, assignment_id: str):
    """Handle allocation delete by assignment command."""
    count = allocation_store.delete_by_assignment(assignment_id)
    output_json({"deleted": count})


def validate_allocation_delete_ids_payload(uuids: list[str]) -> None:
    """Validate delete by IDs - all must exist, no empty strings."""
    for uuid in uuids:
        if len(uuid) == 0:
            raise ValidationError("Empty UUID in list")


def cmd_allocation_delete_ids(allocation_store: AllocationStore, uuids_str: str):
    """Handle allocation delete by IDs command."""
    # Split on comma, preserve empty elements
    uuids = uuids_str.split(",") if uuids_str else [""]

    try:
        validate_allocation_delete_ids_payload(uuids)
    except ForgeError as e:
        output_error(e)

    count, unresolved = allocation_store.delete_by_ids(uuids)
    if unresolved:
        output_error(NotFoundError(f"Unresolved UUIDs: {', '.join(unresolved)}"))
    output_json({"deleted": count})


# ============== MODULE COMMANDS ==============

def validate_module_create_payload(data: Any) -> tuple[str, str, Optional[str]]:
    """Validate module create payload. Returns (unit_uuid, component_type, firmware_image)."""
    collector = ValidationErrorCollector()

    # Payload must be an object
    if not isinstance(data, dict):
        collector.add("payload must be an object")

    collector.raise_if_any()

    # unit_uuid is required
    if "unit_uuid" not in data:
        collector.add("unit_uuid is required")
    elif not isinstance(data["unit_uuid"], str):
        collector.add("unit_uuid must be a string")
    elif len(data["unit_uuid"]) == 0:
        collector.add("unit_uuid must be non-empty")

    # component_type is required
    if "component_type" not in data:
        collector.add("component_type is required")
    elif not isinstance(data["component_type"], str):
        collector.add("component_type must be a string")
    elif len(data["component_type"]) == 0:
        collector.add("component_type must be non-empty")

    # firmware_image is optional
    firmware_image: Optional[str] = None
    if "firmware_image" in data:
        firmware_image = data["firmware_image"]

    collector.raise_if_any()

    return data["unit_uuid"], data["component_type"], firmware_image


def cmd_module_create(module_store: ModuleStore, stdin_data: str):
    """Handle module create command."""
    try:
        data = json.loads(stdin_data)
    except json.JSONDecodeError:
        output_error(ValidationError("stdin is not valid JSON"))

    try:
        unit_uuid, component_type, firmware_image = validate_module_create_payload(data)
        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        module = module_store.create(unit_uuid, component_type, firmware_image)
        output_json(module.to_dict())
    except ForgeError as e:
        output_error(e)


def cmd_module_list(module_store: ModuleStore, unit_filter: Optional[str] = None, filters: Optional[list[tuple[str, str]]] = None):
    """Handle module list command."""
    modules = module_store.list_all(unit_filter=unit_filter, filters=filters)
    output_json([m.to_dict() for m in modules])


def cmd_module_get(module_store: ModuleStore, uuid: str):
    """Handle module get command."""
    module = module_store.get_by_uuid(uuid)
    if module is None:
        output_error(NotFoundError(f"Module '{uuid}' not found"))
    output_json(module.to_dict())


def cmd_module_flash(module_store: ModuleStore, uuid: str, image_id: str):
    """Handle module flash command."""
    try:
        module = module_store.flash(uuid, image_id)
        output_json(module.to_dict())
    except ForgeError as e:
        output_error(e)


# ============== TAG COMMANDS ==============

def validate_tag_create_payload(data: Any) -> tuple[str, str, str]:
    """Validate tag create payload. Returns (module_uuid, key, value)."""
    collector = ValidationErrorCollector()

    # Payload must be an object
    if not isinstance(data, dict):
        collector.add("payload must be an object")

    collector.raise_if_any()

    # module_uuid is required
    if "module_uuid" not in data:
        collector.add("module_uuid is required")
    elif not isinstance(data["module_uuid"], str):
        collector.add("module_uuid must be a string")
    elif len(data["module_uuid"]) == 0:
        collector.add("module_uuid must be non-empty")

    # key is required
    if "key" not in data:
        collector.add("key is required")
    elif not isinstance(data["key"], str):
        collector.add("key must be a string")
    elif len(data["key"]) == 0:
        collector.add("key must be non-empty")

    # value is required
    if "value" not in data:
        collector.add("value is required")
    elif not isinstance(data["value"], str):
        collector.add("value must be a string")
    elif len(data["value"]) == 0:
        collector.add("value must be non-empty")

    collector.raise_if_any()

    return data["module_uuid"], data["key"], data["value"]


def cmd_tag_create(tag_store: TagStore, stdin_data: str):
    """Handle tag create command."""
    try:
        data = json.loads(stdin_data)
    except json.JSONDecodeError:
        output_error(ValidationError("stdin is not valid JSON"))

    try:
        module_uuid, key, value = validate_tag_create_payload(data)
        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        tag = tag_store.create(module_uuid, key, value)
        output_json(tag.to_dict())
    except ForgeError as e:
        output_error(e)


def cmd_tag_list(tag_store: TagStore, module_filter: Optional[str] = None, key_filter: Optional[str] = None):
    """Handle tag list command."""
    tags = tag_store.list_all(module_filter=module_filter, key_filter=key_filter)
    output_json([t.to_dict() for t in tags])


def cmd_tag_get(tag_store: TagStore, uuid: str):
    """Handle tag get command."""
    tag = tag_store.get_by_uuid(uuid)
    if tag is None:
        output_error(NotFoundError(f"Tag '{uuid}' not found"))
    output_json(tag.to_dict())


def cmd_tag_delete(tag_store: TagStore, uuid: str):
    """Handle tag delete command."""
    try:
        tag = tag_store.delete_by_uuid(uuid)
        output_json({"deleted": 1})
    except ForgeError as e:
        output_error(e)




def main():
    parser = argparse.ArgumentParser(
        prog="forge",
        description="Forge CLI tool"
    )
    parser.add_argument(
        "--data-dir",
        required=True,
        help="Directory for persistent state"
    )
    parser.add_argument(
        "resource_type",
        choices=["blueprint", "allocation", "unit", "ledger"],
        help="Resource type command group"
    )

    args, remaining = parser.parse_known_args()

    if not remaining:
        print(f"Error: No {args.resource_type} command specified", file=sys.stderr)
        sys.exit(1)

    command = remaining[0]
    data_dir = Path(args.data_dir)
    blueprint_store = BlueprintStore(data_dir)
    allocation_store = AllocationStore(data_dir)
    unit_store = UnitStore(data_dir)

    if args.resource_type == "blueprint":
        if command == "create":
            stdin_data = sys.stdin.read()
            cmd_create(blueprint_store, stdin_data)

        elif command == "list":
            cmd_list(blueprint_store)

        elif command == "get":
            if len(remaining) < 2:
                print("Error: UUID required for get command", file=sys.stderr)
                sys.exit(1)
            cmd_get(blueprint_store, remaining[1])

        elif command == "delete":
            if len(remaining) < 2:
                print("Error: UUID or --names required for delete command", file=sys.stderr)
                sys.exit(1)

            if remaining[1] == "--names":
                if len(remaining) < 3:
                    print("Error: Names list required with --names", file=sys.stderr)
                    sys.exit(1)
                cmd_delete_names(blueprint_store, remaining[2])
            else:
                cmd_delete_uuid(blueprint_store, remaining[1])

        else:
            print(f"Error: Unknown blueprint command '{command}'", file=sys.stderr)
            sys.exit(1)

    elif args.resource_type == "allocation":
        if command == "bind":
            stdin_data = sys.stdin.read()
            cmd_allocation_bind(allocation_store, stdin_data)

        elif command == "delete":
            assignment_id = None
            ids_str = None

            i = 1
            while i < len(remaining):
                if remaining[i] == "--assignment" and i + 1 < len(remaining):
                    assignment_id = remaining[i + 1]
                    i += 2
                elif remaining[i] == "--ids" and i + 1 < len(remaining):
                    ids_str = remaining[i + 1]
                    i += 2
                else:
                    print(f"Error: Unknown option '{remaining[i]}'", file=sys.stderr)
                    sys.exit(1)

            if (assignment_id is None and ids_str is None) or (assignment_id is not None and ids_str is not None):
                print("Error: Exactly one of --assignment or --ids must be provided", file=sys.stderr)
                sys.exit(1)

            if assignment_id is not None:
                cmd_allocation_delete_assignment(allocation_store, assignment_id)
            else:
                cmd_allocation_delete_ids(allocation_store, ids_str)

        elif command == "create":
            stdin_data = sys.stdin.read()
            cmd_allocation_create(blueprint_store, allocation_store, stdin_data)

        elif command == "list":
            # Parse optional filters
            assignment_id = None
            status = None
            i = 1
            while i < len(remaining):
                if remaining[i] == "--assignment" and i + 1 < len(remaining):
                    assignment_id = remaining[i + 1]
                    i += 2
                elif remaining[i] == "--status" and i + 1 < len(remaining):
                    status = remaining[i + 1]
                    i += 2
                else:
                    print(f"Error: Unknown option '{remaining[i]}'", file=sys.stderr)
                    sys.exit(1)
            cmd_allocation_list(allocation_store, assignment_id=assignment_id, status=status)

        elif command == "get":
            if len(remaining) < 2:
                print("Error: UUID required for get command", file=sys.stderr)
                sys.exit(1)
            cmd_allocation_get(allocation_store, remaining[1])

        else:
            print(f"Error: Unknown allocation command '{command}'", file=sys.stderr)
            sys.exit(1)

    elif args.resource_type == "unit":
        if command == "create":
            stdin_data = sys.stdin.read()
            cmd_unit_create(unit_store, stdin_data)

        elif command == "list":
            cmd_unit_list(unit_store)

        elif command == "get":
            if len(remaining) < 2:
                print("Error: UUID required for get command", file=sys.stderr)
                sys.exit(1)
            cmd_unit_get(unit_store, remaining[1])

        elif command == "activate":
            if len(remaining) < 2:
                print("Error: UUID required for activate command", file=sys.stderr)
                sys.exit(1)
            cmd_unit_activate(unit_store, remaining[1])

        elif command == "deactivate":
            if len(remaining) < 2:
                print("Error: UUID required for deactivate command", file=sys.stderr)
                sys.exit(1)
            cmd_unit_deactivate(unit_store, remaining[1])

        else:
            print(f"Error: Unknown unit command '{command}'", file=sys.stderr)
            sys.exit(1)

    elif args.resource_type == "ledger":
        if command == "capabilities":
            cmd_ledger_capabilities(unit_store)

        else:
            print(f"Error: Unknown ledger command '{command}'", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
