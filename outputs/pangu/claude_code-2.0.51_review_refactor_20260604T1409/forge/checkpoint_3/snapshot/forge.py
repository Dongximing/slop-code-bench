#!/usr/bin/env python3

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class ForgeError(Exception):
    """Base error for Forge commands."""
    def __init__(self, error_type: str, detail: Any):
        self.error_type = error_type
        self.detail = detail
        super().__init__(json.dumps({"error": error_type, "detail": detail}))


def exit_error(error_type: str, detail: Any) -> None:
    """Write error to stdout and exit with code 1."""
    print(json.dumps({"error": error_type, "detail": detail}))
    sys.exit(1)


def generate_uuid() -> str:
    """Generate a lowercase hyphenated UUID v4."""
    return str(uuid.uuid4()).lower()


def get_timestamp() -> str:
    """Get current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(data_dir: Path, filename: str) -> Dict[str, Any]:
    """Load JSON data from the data directory."""
    filepath = data_dir / filename
    if not filepath.exists():
        return {}
    try:
        with open(filepath, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def save_json(data_dir: Path, filename: str, data: Dict[str, Any]) -> None:
    """Save JSON data to the data directory."""
    data_dir.mkdir(parents=True, exist_ok=True)
    filepath = data_dir / filename
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2)


def validate_requirement_set(rs: Any, path: str) -> List[str]:
    """Validate a requirement set object. Returns list of error messages."""
    errors = []

    if not isinstance(rs, dict):
        errors.append(f"{path} must be an object")
        return errors

    # Check for unsupported keys
    allowed_keys = {"resource_type", "resource_count", "capabilities"}
    for key in rs:
        if key not in allowed_keys:
            errors.append(f"{path}.{key} is not supported")

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
        if not isinstance(rc, int) or type(rc) is bool or rc < 1:
            errors.append(f"{path}.resource_count must be a positive integer")

    # Validate capabilities if present
    if "capabilities" in rs:
        caps = rs["capabilities"]
        if not isinstance(caps, list):
            errors.append(f"{path}.capabilities must be an array of strings")
        else:
            for i, cap in enumerate(caps):
                if not isinstance(cap, str):
                    errors.append(f"{path}.capabilities[{i}] must be a string")

    return errors


def validate_blueprint_input(data: Any) -> Tuple[bool, List[str]]:
    """Validate blueprint creation input. Returns (is_valid, errors)."""
    errors = []

    if not isinstance(data, dict):
        errors.append("Input must be a JSON object")
        return False, errors

    # Validate name
    if "name" not in data:
        errors.append("name is required")
    elif not isinstance(data["name"], str) or data["name"] == "":
        errors.append("name must be a non-empty string")

    # Validate requirement_sets
    if "requirement_sets" not in data:
        errors.append("requirement_sets is required")
    elif not isinstance(data["requirement_sets"], list) or len(data["requirement_sets"]) == 0:
        errors.append("requirement_sets must be a non-empty array")
    else:
        for i, rs in enumerate(data["requirement_sets"]):
            errors.extend(validate_requirement_set(rs, f"requirement_sets[{i}]"))

    return len(errors) == 0, errors


def cmd_create(data_dir: Path, input_data: Dict[str, Any]) -> Dict[str, Any]:
    """Create a new blueprint."""
    # Validate input
    is_valid, errors = validate_blueprint_input(input_data)
    if not is_valid:
        exit_error("validation_error", errors)

    name = input_data["name"]
    requirement_sets = input_data["requirement_sets"]

    # Check for existing blueprint with same name
    blueprints = load_json(data_dir, "blueprints.json")
    for bp in blueprints.values():
        if bp["name"] == name:
            exit_error("conflict", f"Blueprint with name '{name}' already exists")

    # Create new blueprint
    blueprint_id = generate_uuid()
    blueprint = {
        "uuid": blueprint_id,
        "name": name,
        "requirement_sets": requirement_sets,
        "created_at": get_timestamp()
    }

    blueprints[blueprint_id] = blueprint
    save_json(data_dir, "blueprints.json", blueprints)

    return blueprint


def cmd_list(data_dir: Path) -> List[Dict[str, Any]]:
    """List all blueprints."""
    blueprints = load_json(data_dir, "blueprints.json")

    # Sort by created_at ascending, then name ascending, then uuid ascending
    sorted_bp = sorted(
        blueprints.values(),
        key=lambda bp: (bp["created_at"], bp["name"], bp["uuid"])
    )

    return sorted_bp


def cmd_get(data_dir: Path, uuid_str: str) -> Dict[str, Any]:
    """Get a blueprint by UUID."""
    blueprints = load_json(data_dir, "blueprints.json")

    if uuid_str not in blueprints:
        exit_error("not_found", f"Blueprint with uuid '{uuid_str}' not found")

    return blueprints[uuid_str]


def cmd_delete_uuid(data_dir: Path, uuid_str: str) -> Dict[str, int]:
    """Delete a blueprint by UUID."""
    blueprints = load_json(data_dir, "blueprints.json")

    if uuid_str not in blueprints:
        exit_error("not_found", f"Blueprint with uuid '{uuid_str}' not found")

    del blueprints[uuid_str]
    save_json(data_dir, "blueprints.json", blueprints)

    return {"deleted": 1}


def cmd_delete_names(data_dir: Path, names: str) -> Dict[str, int]:
    """Delete blueprints by names."""
    name_list = names.split(",")
    blueprints = load_json(data_dir, "blueprints.json")

    # Build name to uuid mapping
    name_to_uuid = {bp["name"]: bp_uuid for bp_uuid, bp in blueprints.items()}

    # Check all names exist
    unresolved = [name for name in name_list if name not in name_to_uuid]
    if unresolved:
        exit_error("not_found", f"Blueprints with names '{','.join(unresolved)}' not found")

    # Delete all specified blueprints
    for name in name_list:
        del blueprints[name_to_uuid[name]]

    save_json(data_dir, "blueprints.json", blueprints)

    return {"deleted": len(name_list)}


def cmd_allocation_create(data_dir: Path, input_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Create allocations from a blueprint."""
    # Validate input payload
    if not isinstance(input_data, dict):
        exit_error("validation_error", "Invalid payload shape")

    if "blueprint_name" not in input_data:
        exit_error("validation_error", "Missing 'blueprint_name' field")

    blueprint_name = input_data["blueprint_name"]

    if not isinstance(blueprint_name, str):
        exit_error("validation_error", "'blueprint_name' must be a string")

    if blueprint_name == "":
        exit_error("validation_error", "'blueprint_name' cannot be an empty string")

    # Look up blueprint by name
    blueprints = load_json(data_dir, "blueprints.json")
    blueprint = None
    for bp in blueprints.values():
        if bp["name"] == blueprint_name:
            blueprint = bp
            break

    if blueprint is None:
        exit_error("not_found", f"Blueprint with name '{blueprint_name}' does not exist")

    # Create allocations
    allocations = load_json(data_dir, "allocations.json")
    created_at = get_timestamp()
    created_allocations = []

    requirement_sets = blueprint["requirement_sets"]

    for idx, rs in enumerate(requirement_sets):
        # Check if this requirement set has category=TYPE_A
        capabilities = rs.get("capabilities", [])
        has_type_a = "category=TYPE_A" in capabilities

        # Determine how many allocations to create
        num_allocations = 2 if has_type_a else 1

        for _ in range(num_allocations):
            alloc_uuid = generate_uuid()
            allocation = {
                "uuid": alloc_uuid,
                "blueprint_name": blueprint_name,
                "requirement_set_index": idx,
                "binding_status": "unbound",
                "assignment_id": None,
                "created_at": created_at
            }
            allocations[alloc_uuid] = allocation
            created_allocations.append(allocation)

    save_json(data_dir, "allocations.json", allocations)

    # Sort by requirement_set_index ascending (multiplied allocations are consecutive)
    created_allocations.sort(key=lambda a: a["requirement_set_index"])

    return created_allocations


def cmd_allocation_list(data_dir: Path, assignment_filter: Optional[str] = None, status_filter: Optional[str] = None) -> List[Dict[str, Any]]:
    """List allocations with optional filters."""
    allocations = load_json(data_dir, "allocations.json")
    alloc_list = list(allocations.values())

    # Apply filters
    if assignment_filter is not None:
        alloc_list = [a for a in alloc_list if a["assignment_id"] == assignment_filter]

    if status_filter is not None:
        alloc_list = [a for a in alloc_list if a["binding_status"] == status_filter]

    # Sort by created_at ascending, then uuid ascending lexicographic
    alloc_list.sort(key=lambda a: (a["created_at"], a["uuid"]))

    return alloc_list


def cmd_allocation_get(data_dir: Path, uuid_str: str) -> Dict[str, Any]:
    """Get an allocation by UUID."""
    allocations = load_json(data_dir, "allocations.json")

    if uuid_str not in allocations:
        exit_error("not_found", f"Allocation with uuid '{uuid_str}' not found")

    return allocations[uuid_str]


def cmd_allocation_bind(data_dir: Path, input_data: Dict[str, Any]) -> Dict[str, str]:
    """Bind or unbind allocations from an assignment."""
    # Validation checks in priority order

    # Check 1: validation_error - missing assignment_id, missing allocations, or empty allocations
    if "assignment_id" not in input_data:
        exit_error("validation_error", "Missing 'assignment_id' field")

    if "allocations" not in input_data:
        exit_error("validation_error", "Missing 'allocations' field")

    allocations_input = input_data["allocations"]
    if not isinstance(allocations_input, dict) or len(allocations_input) == 0:
        exit_error("validation_error", "allocations must be a non-empty object")

    assignment_id = input_data["assignment_id"]

    # Check 2: invalid_operation - operation value must be "add" or "remove"
    valid_ops = {"add", "remove"}
    for alloc_uuid, operation in allocations_input.items():
        if operation not in valid_ops:
            exit_error("invalid_operation", f"Invalid operation '{operation}' for allocation '{alloc_uuid}'")

    # Load current allocations
    allocations = load_json(data_dir, "allocations.json")

    # Check 3: not_found - one or more allocation UUIDs do not exist
    for alloc_uuid in allocations_input.keys():
        if alloc_uuid not in allocations:
            exit_error("not_found", f"Allocation with uuid '{alloc_uuid}' not found")

    # Check 4: duplicate_assignment guard
    # If assignment_id has existing bound allocations and payload contains at least one "add", fail
    has_add = any(op == "add" for op in allocations_input.values())
    if has_add:
        for alloc in allocations.values():
            if alloc.get("assignment_id") == assignment_id and alloc.get("binding_status") == "bound":
                exit_error("duplicate_assignment", f"Assignment '{assignment_id}' already has bound allocations")

    # Apply all operations atomically
    for alloc_uuid, operation in allocations_input.items():
        alloc = allocations[alloc_uuid]
        if operation == "add":
            alloc["binding_status"] = "bound"
            alloc["assignment_id"] = assignment_id
        elif operation == "remove":
            # Silent no-op if already unbound
            if alloc["binding_status"] != "unbound" or alloc.get("assignment_id") is not None:
                alloc["binding_status"] = "unbound"
                alloc["assignment_id"] = None

    save_json(data_dir, "allocations.json", allocations)

    return {"status": "accepted"}


def cmd_allocation_delete(data_dir: Path, assignment: Optional[str] = None, ids: Optional[str] = None) -> Dict[str, int]:
    """Delete allocations by assignment ID or by UUID list."""
    # Validate exactly one of assignment or ids is provided
    if (assignment is None and ids is None) or (assignment is not None and ids is not None):
        exit_error("validation_error", "Exactly one of --assignment or --ids must be provided")

    allocations = load_json(data_dir, "allocations.json")

    if assignment is not None:
        # Assignment-based deletion: delete all allocations with matching assignment_id
        deleted_count = 0
        allocs_to_delete = []
        for alloc_uuid, alloc in allocations.items():
            if alloc.get("assignment_id") == assignment:
                allocs_to_delete.append(alloc_uuid)
                deleted_count += 1

        for alloc_uuid in allocs_to_delete:
            del allocations[alloc_uuid]

        save_json(data_dir, "allocations.json", allocations)
        return {"deleted": deleted_count}

    else:
        # ID-list deletion
        uuid_list = [uid.strip() for uid in ids.split(",")]

        # Check all UUIDs exist - empty CSV members are treated as unresolved UUIDs
        unresolved = [uid for uid in uuid_list if uid not in allocations]
        if unresolved:
            exit_error("not_found", f"Allocations with uuids '{','.join(unresolved)}' not found")

        # Delete all specified allocations
        for alloc_uuid in uuid_list:
            del allocations[alloc_uuid]

        save_json(data_dir, "allocations.json", allocations)
        return {"deleted": len(uuid_list)}


def main():
    parser = argparse.ArgumentParser(
        description="Forge - Blueprint management CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--data-dir", required=True, help="Directory for persistent state")

    # Top level subcommands with bp_ prefix to avoid conflict
    subparsers = parser.add_subparsers(dest="command", required=True)

    # blueprint subcommand
    blueprint_parser = subparsers.add_parser("blueprint", help="Blueprint management")
    blueprint_subparsers = blueprint_parser.add_subparsers(dest="subcmd", required=True)

    # blueprint create
    create_cmd = blueprint_subparsers.add_parser("create", help="Create a new blueprint")
    create_cmd.set_defaults(func=lambda args, data_dir: cmd_create(data_dir, json.load(sys.stdin)))

    # blueprint list
    list_cmd = blueprint_subparsers.add_parser("list", help="List all blueprints")
    list_cmd.set_defaults(func=lambda args, data_dir: cmd_list(data_dir))

    # blueprint get
    get_cmd = blueprint_subparsers.add_parser("get", help="Get a blueprint by UUID")
    get_cmd.add_argument("uuid", help="Blueprint UUID")
    get_cmd.set_defaults(func=lambda args, data_dir: cmd_get(data_dir, args.uuid))

    # blueprint delete
    delete_cmd = blueprint_subparsers.add_parser("delete", help="Delete a blueprint")
    delete_mutex = delete_cmd.add_mutually_exclusive_group(required=True)
    delete_mutex.add_argument("uuid", nargs="?", help="Blueprint UUID")
    delete_mutex.add_argument("--names", help="Comma-separated list of blueprint names")
    delete_cmd.set_defaults(func=lambda args, data_dir: (
        cmd_delete_uuid(data_dir, args.uuid) if args.uuid
        else cmd_delete_names(data_dir, args.names)
    ))

    # allocation subcommand
    allocation_parser = subparsers.add_parser("allocation", help="Allocation management")
    allocation_subparsers = allocation_parser.add_subparsers(dest="subcmd", required=True)

    # allocation create
    allocation_create_cmd = allocation_subparsers.add_parser("create", help="Create allocations from a blueprint")
    allocation_create_cmd.set_defaults(func=lambda args, data_dir: cmd_allocation_create(data_dir, json.load(sys.stdin)))

    # allocation list
    allocation_list_cmd = allocation_subparsers.add_parser("list", help="List all allocations")
    allocation_list_cmd.add_argument("--assignment", dest="assignment", help="Filter by assignment ID")
    allocation_list_cmd.add_argument("--status", dest="status", help="Filter by binding status")
    allocation_list_cmd.set_defaults(func=lambda args, data_dir: cmd_allocation_list(data_dir, args.assignment, args.status))

    # allocation get
    allocation_get_cmd = allocation_subparsers.add_parser("get", help="Get an allocation by UUID")
    allocation_get_cmd.add_argument("uuid", help="Allocation UUID")
    allocation_get_cmd.set_defaults(func=lambda args, data_dir: cmd_allocation_get(data_dir, args.uuid))

    # allocation bind
    allocation_bind_cmd = allocation_subparsers.add_parser("bind", help="Bind or unbind allocations from an assignment")
    allocation_bind_cmd.set_defaults(func=lambda args, data_dir: cmd_allocation_bind(data_dir, json.load(sys.stdin)))

    # allocation delete
    allocation_delete_cmd = allocation_subparsers.add_parser("delete", help="Delete allocations")
    delete_mutex = allocation_delete_cmd.add_mutually_exclusive_group(required=True)
    delete_mutex.add_argument("--assignment", help="Delete all allocations with this assignment ID")
    delete_mutex.add_argument("--ids", help="Comma-separated list of allocation UUIDs to delete")
    allocation_delete_cmd.set_defaults(func=lambda args, data_dir: cmd_allocation_delete(data_dir, args.assignment, args.ids))

    args = parser.parse_args()

    data_dir = Path(args.data_dir)

    try:
        result = args.func(args, data_dir)
        # For specific commands, output JSON without indent as per specification
        if isinstance(result, dict) and "status" in result:
            # Bind command
            print('{"status": "accepted"}')
        elif isinstance(result, dict) and "deleted" in result:
            # Delete command
            print(f'{{"deleted": {result["deleted"]}}}')
        else:
            print(json.dumps(result, indent=2))
        sys.exit(0)
    except json.JSONDecodeError:
        exit_error("validation_error", "Invalid JSON input")
    except ForgeError as e:
        exit_error(e.error_type, e.detail)


if __name__ == "__main__":
    main()
