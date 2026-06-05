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
    """Validation error."""
    error = "validation_error"


class NotFoundError(ForgeError):
    """Not found error."""
    error = "not_found"


class ConflictError(ForgeError):
    """Conflict error."""
    error = "conflict"


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


class ValidationErrorCollector:
    """Collects validation errors and reports them all at once."""

    def __init__(self):
        self.errors: list[str] = []

    def add(self, message: str):
        self.errors.append(message)

    def raise_if_any(self):
        if self.errors:
            raise ValidationError("; ".join(self.errors))


class BlueprintStore:
    """Persistent storage for blueprints."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.file_path = self.data_dir / "blueprints.json"
        self._blueprints: dict[str, Blueprint] = {}
        self._name_to_uuid: dict[str, str] = {}
        self._load()

    def _load(self):
        """Load blueprints from disk."""
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
        """Save blueprints to disk."""
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


def main():
    parser = argparse.ArgumentParser(
        prog="forge",
        description="Forge CLI tool for managing blueprints"
    )
    parser.add_argument(
        "--data-dir",
        required=True,
        help="Directory for persistent state"
    )
    parser.add_argument(
        "blueprint",
        help="Blueprint command group"
    )

    args, remaining = parser.parse_known_args()

    if args.blueprint != "blueprint":
        print("Error: Expected 'blueprint' as second argument", file=sys.stderr)
        sys.exit(1)

    if not remaining:
        print("Error: No blueprint command specified", file=sys.stderr)
        sys.exit(1)

    command = remaining[0]
    store = BlueprintStore(Path(args.data_dir))

    if command == "create":
        stdin_data = sys.stdin.read()
        cmd_create(store, stdin_data)

    elif command == "list":
        cmd_list(store)

    elif command == "get":
        if len(remaining) < 2:
            print("Error: UUID required for get command", file=sys.stderr)
            sys.exit(1)
        cmd_get(store, remaining[1])

    elif command == "delete":
        if len(remaining) < 2:
            print("Error: UUID or --names required for delete command", file=sys.stderr)
            sys.exit(1)

        if remaining[1] == "--names":
            if len(remaining) < 3:
                print("Error: Names list required with --names", file=sys.stderr)
                sys.exit(1)
            cmd_delete_names(store, remaining[2])
        else:
            cmd_delete_uuid(store, remaining[1])

    else:
        print(f"Error: Unknown command '{command}'", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
