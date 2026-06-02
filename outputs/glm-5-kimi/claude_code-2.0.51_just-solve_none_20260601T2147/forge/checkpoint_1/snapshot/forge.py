#!/usr/bin/env python3
"""Forge - Blueprint management CLI tool."""

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


def generate_uuid():
    """Generate a lowercase hyphenated UUID v4."""
    return str(uuid.uuid4())


def generate_timestamp():
    """Generate ISO 8601 UTC timestamp with second precision."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_error(category, detail):
    """Write error JSON to stdout and exit with code 1."""
    error_obj = {"error": category, "detail": detail}
    print(json.dumps(error_obj))
    sys.exit(1)


def write_success(data):
    """Write success JSON to stdout and exit with code 0."""
    print(json.dumps(data))
    sys.exit(0)


def load_blueprints(data_dir):
    """Load all blueprints from the data directory."""
    blueprints = []
    data_path = Path(data_dir)
    if not data_path.exists():
        return blueprints

    for file_path in data_path.glob("*.json"):
        with open(file_path, "r") as f:
            blueprint = json.load(f)
            blueprints.append(blueprint)

    return blueprints


def save_blueprint(data_dir, blueprint):
    """Save a blueprint to the data directory."""
    data_path = Path(data_dir)
    data_path.mkdir(parents=True, exist_ok=True)

    file_path = data_path / f"{blueprint['uuid']}.json"
    with open(file_path, "w") as f:
        json.dump(blueprint, f)


def delete_blueprint_file(data_dir, blueprint_uuid):
    """Delete a blueprint file from the data directory."""
    data_path = Path(data_dir)
    file_path = data_path / f"{blueprint_uuid}.json"
    if file_path.exists():
        file_path.unlink()
        return True
    return False


def find_blueprint_by_uuid(blueprints, target_uuid):
    """Find a blueprint by UUID."""
    for bp in blueprints:
        if bp["uuid"] == target_uuid:
            return bp
    return None


def find_blueprint_by_name(blueprints, target_name):
    """Find a blueprint by name."""
    for bp in blueprints:
        if bp["name"] == target_name:
            return bp
    return None


def validate_requirement_set(req_set, index, errors):
    """Validate a single requirement set."""
    # Check for unsupported keys
    supported_keys = {"resource_type", "resource_count", "capabilities"}
    for key in req_set.keys():
        if key not in supported_keys:
            errors.append(f"requirement_sets[{index}] contains unsupported key '{key}'")

    # Validate resource_type
    if "resource_type" not in req_set:
        errors.append(f"requirement_sets[{index}].resource_type is required")
    elif not isinstance(req_set["resource_type"], str):
        errors.append(f"requirement_sets[{index}].resource_type must be a string")

    # Validate resource_count
    if "resource_count" not in req_set:
        errors.append(f"requirement_sets[{index}].resource_count is required")
    elif not isinstance(req_set["resource_count"], int) or isinstance(req_set["resource_count"], bool):
        errors.append(f"requirement_sets[{index}].resource_count must be an integer")
    elif req_set["resource_count"] < 1:
        errors.append(f"requirement_sets[{index}].resource_count must be positive")

    # Validate capabilities if present
    if "capabilities" in req_set:
        capabilities = req_set["capabilities"]
        if not isinstance(capabilities, list):
            errors.append(f"requirement_sets[{index}].capabilities must be an array")
        else:
            for i, cap in enumerate(capabilities):
                if not isinstance(cap, str):
                    errors.append(f"requirement_sets[{index}].capabilities[{i}] must be a string")


def validate_blueprint_input(data):
    """Validate blueprint input and return list of errors."""
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
    else:
        for i, req_set in enumerate(data["requirement_sets"]):
            if not isinstance(req_set, dict):
                errors.append(f"requirement_sets[{i}] must be an object")
            else:
                validate_requirement_set(req_set, i, errors)

    return errors


def build_requirement_set(req_set):
    """Build a requirement set for output, preserving only provided keys."""
    result = {}
    result["resource_type"] = req_set["resource_type"]
    result["resource_count"] = req_set["resource_count"]
    if "capabilities" in req_set:
        result["capabilities"] = req_set["capabilities"]
    return result


def cmd_create(data_dir):
    """Handle blueprint create command."""
    # Read JSON from stdin
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        write_error("validation_error", "Invalid JSON")

    # Validate input
    errors = validate_blueprint_input(data)
    if errors:
        write_error("validation_error", "; ".join(errors))

    # Check for conflict
    blueprints = load_blueprints(data_dir)
    existing = find_blueprint_by_name(blueprints, data["name"])
    if existing:
        write_error("conflict", data["name"])

    # Create blueprint
    new_blueprint = {
        "uuid": generate_uuid(),
        "name": data["name"],
        "requirement_sets": [build_requirement_set(rs) for rs in data["requirement_sets"]],
        "created_at": generate_timestamp()
    }

    # Save blueprint
    save_blueprint(data_dir, new_blueprint)

    write_success(new_blueprint)


def cmd_list(data_dir):
    """Handle blueprint list command."""
    blueprints = load_blueprints(data_dir)

    # Sort: created_at ascending, then name ascending, then uuid ascending
    blueprints.sort(key=lambda bp: (bp["created_at"], bp["name"], bp["uuid"]))

    write_success(blueprints)


def cmd_get(data_dir, target_uuid):
    """Handle blueprint get command."""
    blueprints = load_blueprints(data_dir)
    blueprint = find_blueprint_by_uuid(blueprints, target_uuid)

    if not blueprint:
        write_error("not_found", target_uuid)

    write_success(blueprint)


def cmd_delete_single(data_dir, target_uuid):
    """Handle blueprint delete command for single UUID."""
    blueprints = load_blueprints(data_dir)
    blueprint = find_blueprint_by_uuid(blueprints, target_uuid)

    if not blueprint:
        write_error("not_found", target_uuid)

    delete_blueprint_file(data_dir, target_uuid)
    write_success({"deleted": 1})


def cmd_delete_names(data_dir, names_csv):
    """Handle blueprint delete command with --names flag."""
    # Parse CSV
    names = names_csv.split(",")

    blueprints = load_blueprints(data_dir)

    # Build name to blueprint mapping
    name_to_blueprint = {}
    for bp in blueprints:
        name_to_blueprint[bp["name"]] = bp

    # Check all names exist
    unresolved = []
    for name in names:
        if name not in name_to_blueprint:
            unresolved.append(name)

    if unresolved:
        write_error("not_found", unresolved)

    # Delete all blueprints
    count = 0
    for name in names:
        bp = name_to_blueprint[name]
        delete_blueprint_file(data_dir, bp["uuid"])
        count += 1

    write_success({"deleted": count})


def main():
    parser = argparse.ArgumentParser(description="Forge - Blueprint management CLI tool")
    parser.add_argument("--data-dir", required=True, help="Directory for persistent state")

    subparsers = parser.add_subparsers(dest="resource_command")

    # blueprint subcommand
    blueprint_parser = subparsers.add_parser("blueprint", help="Blueprint operations")
    blueprint_subparsers = blueprint_parser.add_subparsers(dest="blueprint_command")

    # blueprint create
    create_parser = blueprint_subparsers.add_parser("create", help="Create a new blueprint")

    # blueprint list
    list_parser = blueprint_subparsers.add_parser("list", help="List all blueprints")

    # blueprint get
    get_parser = blueprint_subparsers.add_parser("get", help="Get a blueprint by UUID")
    get_parser.add_argument("uuid", help="UUID of the blueprint")

    # blueprint delete
    delete_parser = blueprint_subparsers.add_parser("delete", help="Delete blueprint(s)")
    delete_parser.add_argument("uuid", nargs="?", help="UUID of the blueprint to delete")
    delete_parser.add_argument("--names", help="Comma-separated list of blueprint names to delete")

    args = parser.parse_args()

    # Handle blueprint commands
    if args.resource_command != "blueprint" or args.blueprint_command is None:
        parser.print_help()
        sys.exit(1)

    if args.blueprint_command == "create":
        cmd_create(args.data_dir)
    elif args.blueprint_command == "list":
        cmd_list(args.data_dir)
    elif args.blueprint_command == "get":
        cmd_get(args.data_dir, args.uuid)
    elif args.blueprint_command == "delete":
        if args.names is not None:
            cmd_delete_names(args.data_dir, args.names)
        elif args.uuid:
            cmd_delete_single(args.data_dir, args.uuid)
        else:
            write_error("validation_error", "Either UUID or --names must be provided")


if __name__ == "__main__":
    main()
