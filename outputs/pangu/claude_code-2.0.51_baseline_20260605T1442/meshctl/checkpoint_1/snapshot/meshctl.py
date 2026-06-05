#!/usr/bin/env python3
"""meshctl - Manage mesh resources from YAML specs."""

import argparse
import json
import os
import re
import sys
from pathlib import Path

import yaml

# Storage file for persisted resources
STORAGE_FILE = "/tmp/meshes.json"


def load_storage():
    """Load persisted meshes from storage."""
    if os.path.exists(STORAGE_FILE):
        with open(STORAGE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_storage(data):
    """Save meshes to storage."""
    with open(STORAGE_FILE, "w") as f:
        json.dump(data, f, indent=2)


def parse_error(field, message, error_type="invalid"):
    """Create an error object."""
    return {"field": field, "message": message, "type": error_type}


def parse_quantity(value, kind):
    """
    Parse a resource quantity string into a numeric value for comparison.
    Returns (numeric_value, original_string) or raises ValueError.
    For memory: bytes
    For cpu: millicores
    """
    if isinstance(value, (int, float)):
        return float(value), str(value)

    if not isinstance(value, str):
        raise ValueError("Quantity must be string or number")

    value = value.strip()

    if kind == "memory":
        # Match patterns: number, numberKi, numberMi, numberGi, numberTi
        match = re.match(r'^(\d+)(Ki|Mi|Gi|Ti)?$', value, re.IGNORECASE)
        if not match:
            raise ValueError(f"Invalid memory format: {value}")

        num = int(match.group(1))
        suffix = match.group(2)

        if suffix is None:
            # Plain integer = bytes
            return float(num), value
        elif suffix.upper() == "KI":
            return float(num * 1024), value
        elif suffix.upper() == "MI":
            return float(num * 1024 * 1024), value
        elif suffix.upper() == "GI":
            return float(num * 1024 * 1024 * 1024), value
        elif suffix.upper() == "TI":
            return float(num * 1024 * 1024 * 1024 * 1024), value

    elif kind == "cpu":
        # Match patterns: number, numberm
        match = re.match(r'^(\d+)(m)?$', value)
        if not match:
            raise ValueError(f"Invalid CPU format: {value}")

        num = int(match.group(1))
        suffix = match.group(2)

        if suffix is None:
            # Plain integer = cores
            return float(num * 1000), value
        elif suffix == "m":
            # millicores
            return float(num), value

    raise ValueError(f"Unknown quantity kind: {kind}")


def parse_runtime(runtime_str):
    """Parse semantic version string."""
    if not isinstance(runtime_str, str):
        raise ValueError("Runtime must be a string")

    match = re.match(r'^(\d+)\.(\d+)\.(\d+)$', runtime_str)
    if not match:
        raise ValueError(f"Invalid runtime format: {runtime_str}")

    major, minor, patch = map(int, match.groups())
    return {"major": major, "minor": minor, "patch": patch}


def check_forbidden_autoscaling(obj, errors, path="spec"):
    """Recursively check for forbidden autoScaling field."""
    if not isinstance(obj, dict):
        return

    for key, value in obj.items():
        current_path = f"{path}.{key}" if path else key
        if key == "autoScaling":
            errors.append({
                "field": current_path.rstrip('.'),
                "message": "autoScaling is forbidden under spec",
                "type": "forbidden"
            })
        elif isinstance(value, dict):
            check_forbidden_autoscaling(value, errors, current_path)


def validate_yaml(data):
    """
    Validate the YAML data according to specification.
    Returns list of error objects.
    """
    errors = []

    # Check if it's a mapping
    if not isinstance(data, dict):
        return [parse_error("", "Document must be a mapping", "parse")]

    # Check for top-level keys
    if "metadata" not in data or "spec" not in data:
        if "metadata" not in data:
            errors.append(parse_error("metadata", "metadata is required", "required"))
        if "spec" not in data:
            errors.append(parse_error("spec", "spec is required", "required"))
        return errors

    metadata = data.get("metadata") or {}
    spec = data.get("spec") or {}

    # Validate metadata.name
    name = metadata.get("name")
    if not name or not isinstance(name, str) or name.strip() == "":
        errors.append(parse_error("metadata.name", "metadata.name is required and cannot be empty", "required"))
    elif not re.match(r'^[a-z0-9][a-z0-9-]*[a-z0-9]$', name):
        errors.append(parse_error("metadata.name", "metadata.name must match ^[a-z0-9][a-z0-9-]*[a-z0-9]$", "invalid"))
    elif len(name) < 2:
        errors.append(parse_error("metadata.name", "metadata.name must be at least 2 characters", "invalid"))

    # Validate spec.instances
    instances = spec.get("instances")
    if instances is not None:
        if not isinstance(instances, int):
            errors.append(parse_error("spec.instances", "spec.instances must be an integer", "invalid"))
        elif instances <= 0:
            errors.append(parse_error("spec.instances", "spec.instances must be a positive integer", "invalid"))

    # Validate spec.runtime
    runtime = spec.get("runtime")
    if runtime is not None:
        try:
            parse_runtime(runtime)
        except ValueError as e:
            errors.append(parse_error("spec.runtime", str(e), "invalid"))

    # Check for forbidden autoScaling
    check_forbidden_autoscaling(data, errors, "")

    # Validate resources
    resources = spec.get("resources", {})

    # Validate memory
    memory = resources.get("memory")
    if memory is not None:
        if not isinstance(memory, dict):
            errors.append(parse_error("spec.resources.memory", "spec.resources.memory must be an object", "invalid"))
        else:
            limit = memory.get("limit")
            request = memory.get("request")

            if limit is None:
                errors.append(parse_error("spec.resources.memory.limit", "spec.resources.memory.limit is required", "required"))
            else:
                try:
                    limit_val, _ = parse_quantity(limit, "memory")
                    if request is not None:
                        try:
                            request_val, _ = parse_quantity(request, "memory")
                            if request_val > limit_val:
                                errors.append(parse_error(
                                    "spec.resources.memory.request",
                                    "spec.resources.memory.request cannot exceed spec.resources.memory.limit",
                                    "invalid"
                                ))
                        except ValueError as e:
                            errors.append(parse_error("spec.resources.memory.request", str(e), "invalid"))
                except ValueError as e:
                    errors.append(parse_error("spec.resources.memory.limit", str(e), "invalid"))

    # Validate CPU
    cpu = resources.get("cpu")
    if cpu is not None:
        if not isinstance(cpu, dict):
            errors.append(parse_error("spec.resources.cpu", "spec.resources.cpu must be an object", "invalid"))
        else:
            limit = cpu.get("limit")
            request = cpu.get("request")

            if limit is None:
                errors.append(parse_error("spec.resources.cpu.limit", "spec.resources.cpu.limit is required", "required"))
            else:
                try:
                    limit_val, _ = parse_quantity(limit, "cpu")
                    if request is not None:
                        try:
                            request_val, _ = parse_quantity(request, "cpu")
                            if request_val > limit_val:
                                errors.append(parse_error(
                                    "spec.resources.cpu.request",
                                    "spec.resources.cpu.request cannot exceed spec.resources.cpu.limit",
                                    "invalid"
                                ))
                        except ValueError as e:
                            errors.append(parse_error("spec.resources.cpu.request", str(e), "invalid"))
                except ValueError as e:
                    errors.append(parse_error("spec.resources.cpu.limit", str(e), "invalid"))

    # Validate migration.strategy
    migration = spec.get("migration", {})
    if migration is not None:
        strategy = migration.get("strategy")
        if strategy is not None and strategy != "FullStop":
            errors.append(parse_error("spec.migration.strategy", 'spec.migration.strategy must be "FullStop"', "invalid"))

    return errors


def apply_defaults(data):
    """Apply default values to the data."""
    # Handle case where spec is null/None in YAML
    if data.get("spec") is None:
        data["spec"] = {}
    spec = data["spec"]

    # Default instances = 1
    if "instances" not in spec:
        spec["instances"] = 1

    # Default authentication.enabled = true
    access = spec.setdefault("access", {})
    authentication = access.setdefault("authentication", {})
    if "enabled" not in authentication:
        authentication["enabled"] = True

    # Default migration.strategy = "FullStop"
    migration = spec.setdefault("migration", {})
    if "strategy" not in migration:
        migration["strategy"] = "FullStop"

    # Default memory
    resources = spec.setdefault("resources", {})
    memory = resources.setdefault("memory", {})
    if memory == {}:
        memory["limit"] = "1Gi"
        memory["request"] = "1Gi"
    else:
        if "limit" in memory and "request" not in memory:
            memory["request"] = memory["limit"]

    # Default CPU - only if cpu is explicitly provided or present
    # Note: cpu defaults only apply if cpu object is present
    # If not present at all, don't add it
    if "cpu" in resources:
        cpu = resources["cpu"]
        if "limit" in cpu and "request" not in cpu:
            cpu["request"] = cpu["limit"]

    return data


def load_yaml_file(filepath):
    """Load and parse a YAML file."""
    try:
        with open(filepath, "r") as f:
            content = f.read()
            data = yaml.safe_load(content)
            return data, None
    except FileNotFoundError:
        return None, parse_error("", f"File not found: {filepath}", "parse")
    except yaml.YAMLError as e:
        return None, parse_error("", f"Failed to parse YAML: {e}", "parse")


def output_errors(errors):
    """Output errors as JSON to stdout."""
    result = {"errors": errors}
    print(json.dumps(result, indent=2))


def output_json(obj):
    """Output object as JSON to stdout."""
    print(json.dumps(obj, indent=2))


def create_mesh(filepath):
    """Create a mesh from a YAML file."""
    data, error = load_yaml_file(filepath)
    if error:
        output_errors([error])
        return

    # Validate
    errors = validate_yaml(data)
    if errors:
        output_errors(errors)
        return

    # Check for duplicate
    storage = load_storage()
    name = data["metadata"]["name"]
    if name in storage:
        output_errors([parse_error("metadata.name", "A mesh with this name already exists", "duplicate")])
        return

    # Apply defaults
    data = apply_defaults(data)

    # Add status
    data["status"] = {"state": "Running"}

    # Persist
    storage[name] = data
    save_storage(storage)

    # Output full resource
    output_json(data)


def list_meshes():
    """List all meshes."""
    storage = load_storage()
    meshes = []

    for name, mesh_data in storage.items():
        meshes.append({
            "name": name,
            "status": mesh_data.get("status", {"state": "Unknown"})
        })

    # Sort by name ascending, lexicographic, case-sensitive
    meshes.sort(key=lambda x: x["name"])

    output_json(meshes)


def describe_mesh(name):
    """Describe a specific mesh."""
    storage = load_storage()

    if name not in storage:
        output_errors([parse_error("metadata.name", f"Mesh '{name}' not found", "not_found")])
        return

    output_json(storage[name])


def delete_mesh(name):
    """Delete a mesh."""
    storage = load_storage()

    if name not in storage:
        output_errors([parse_error("metadata.name", f"Mesh '{name}' not found", "not_found")])
        return

    del storage[name]
    save_storage(storage)

    output_json({
        "message": f"Mesh '{name}' has been deleted",
        "metadata": {"name": name}
    })


def main():
    parser = argparse.ArgumentParser(
        prog="python meshctl.py",
        description="Manage mesh resources from YAML specs"
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # mesh subcommand
    mesh_parser = subparsers.add_parser("mesh", help="Mesh resource operations")
    mesh_subparsers = mesh_parser.add_subparsers(dest="operation", help="Mesh operations")

    # mesh create
    create_parser = mesh_subparsers.add_parser("create", help="Create a mesh from YAML")
    create_parser.add_argument("-f", "--file", required=True, help="YAML file path")

    # mesh list
    mesh_subparsers.add_parser("list", help="List all meshes")

    # mesh describe
    describe_parser = mesh_subparsers.add_parser("describe", help="Describe a mesh")
    describe_parser.add_argument("name", help="Mesh name")

    # mesh delete
    delete_parser = mesh_subparsers.add_parser("delete", help="Delete a mesh")
    delete_parser.add_argument("name", help="Mesh name")

    args = parser.parse_args()

    if args.command == "mesh":
        if args.operation == "create":
            create_mesh(args.file)
        elif args.operation == "list":
            list_meshes()
        elif args.operation == "describe":
            describe_mesh(args.name)
        elif args.operation == "delete":
            delete_mesh(args.name)
        else:
            parser.print_help()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
