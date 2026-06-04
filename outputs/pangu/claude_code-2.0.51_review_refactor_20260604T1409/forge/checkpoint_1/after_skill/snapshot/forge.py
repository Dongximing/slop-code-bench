#!/usr/bin/env python3

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple


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


def load_blueprints(data_dir: Path) -> Dict[str, Any]:
    """Load blueprints from the data directory."""
    blueprints_file = data_dir / "blueprints.json"
    if not blueprints_file.exists():
        return {}
    try:
        with open(blueprints_file, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def save_blueprints(data_dir: Path, blueprints: Dict[str, Any]) -> None:
    """Save blueprints to the data directory."""
    data_dir.mkdir(parents=True, exist_ok=True)
    blueprints_file = data_dir / "blueprints.json"
    with open(blueprints_file, "w") as f:
        json.dump(blueprints, f, indent=2)


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
    blueprints = load_blueprints(data_dir)
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
    save_blueprints(data_dir, blueprints)

    return blueprint


def cmd_list(data_dir: Path) -> List[Dict[str, Any]]:
    """List all blueprints."""
    blueprints = load_blueprints(data_dir)

    # Sort by created_at ascending, then name ascending, then uuid ascending
    sorted_bp = sorted(
        blueprints.values(),
        key=lambda bp: (bp["created_at"], bp["name"], bp["uuid"])
    )

    return sorted_bp


def cmd_get(data_dir: Path, uuid_str: str) -> Dict[str, Any]:
    """Get a blueprint by UUID."""
    blueprints = load_blueprints(data_dir)

    if uuid_str not in blueprints:
        exit_error("not_found", f"Blueprint with uuid '{uuid_str}' not found")

    return blueprints[uuid_str]


def cmd_delete_uuid(data_dir: Path, uuid_str: str) -> Dict[str, int]:
    """Delete a blueprint by UUID."""
    blueprints = load_blueprints(data_dir)

    if uuid_str not in blueprints:
        exit_error("not_found", f"Blueprint with uuid '{uuid_str}' not found")

    del blueprints[uuid_str]
    save_blueprints(data_dir, blueprints)

    return {"deleted": 1}


def cmd_delete_names(data_dir: Path, names: str) -> Dict[str, int]:
    """Delete blueprints by names."""
    name_list = names.split(",")
    blueprints = load_blueprints(data_dir)

    # Build name to uuid mapping
    name_to_uuid = {bp["name"]: bp_uuid for bp_uuid, bp in blueprints.items()}

    # Check all names exist
    unresolved = [name for name in name_list if name not in name_to_uuid]
    if unresolved:
        exit_error("not_found", f"Blueprints with names '{','.join(unresolved)}' not found")

    # Delete all specified blueprints
    for name in name_list:
        del blueprints[name_to_uuid[name]]

    save_blueprints(data_dir, blueprints)

    return {"deleted": len(name_list)}


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

    args = parser.parse_args()

    data_dir = Path(args.data_dir)

    try:
        result = args.func(args, data_dir)
        print(json.dumps(result, indent=2))
        sys.exit(0)
    except json.JSONDecodeError:
        exit_error("validation_error", "Invalid JSON input")
    except ForgeError as e:
        exit_error(e.error_type, e.detail)


if __name__ == "__main__":
    main()
