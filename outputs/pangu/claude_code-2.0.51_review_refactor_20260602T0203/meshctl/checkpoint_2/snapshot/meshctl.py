#!/usr/bin/env python3
"""Mesh Lifecycle and Topology CLI Tool."""

import argparse
import json
import re
import sys
from enum import Enum
from typing import Any, Optional


def simple_yaml_load(stream: str) -> dict:
    """
    Parse a simplified YAML format.
    Supports nested structures, lists, and basic types.
    """
    lines = stream.split('\n')
    root = {}
    stack = [{"dict": root, "indent": -1}]

    i = 0
    while i < len(lines):
        line = lines[i]

        # Skip empty lines and comments
        if not line.strip() or line.strip().startswith('#'):
            i += 1
            continue

        # Calculate indentation
        indent = 0
        stripped = line
        for char in line:
            if char == ' ':
                indent += 1
            elif char == '\t':
                indent += 4  # Treat tab as 4 spaces
            else:
                break
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # Parse key-value pair
        if ':' not in stripped:
            i += 1
            continue

        key_part, _, value_part = stripped.partition(':')
        key = key_part.strip()
        value_str = value_part.strip()

        # Find correct parent in stack
        while stack and stack[-1]["indent"] >= indent:
            stack.pop()

        if not stack:
            i += 1
            continue

        parent = stack[-1]["dict"]

        # Handle nested structures
        if value_str == '':
            # Could be nested dict or list item
            # Check next line for list item
            if i + 1 < len(lines) and lines[i + 1].strip().startswith('-'):
                # List
                new_list = []
                parent[key] = new_list
                stack.append({"dict": new_list, "indent": indent, "type": "list"})
            else:
                # Nested dict
                new_dict = {}
                if key in parent and isinstance(parent[key], list):
                    # Handle multiple same keys -> list of dicts
                    if not isinstance(parent[key][0], dict):
                        parent[key] = [{}, parent[key][0]]
                    new_dict = {}
                    parent[key].append(new_dict)
                elif key in parent and isinstance(parent[key], dict):
                    # Merge into existing dict
                    new_dict = parent[key]
                else:
                    parent[key] = new_dict
                stack.append({"dict": new_dict, "indent": indent})
        else:
            # Simple key-value
            if key in parent and isinstance(parent[key], list):
                # Append to list
                parent[key].append(parse_yaml_value(value_str))
            else:
                parent[key] = parse_yaml_value(value_str)

        i += 1

    return root


def parse_yaml_value(value: str) -> Any:
    """Parse a YAML scalar value."""
    # Null
    if value.lower() in ('null', '~', ''):
        return None

    # Boolean
    if value.lower() == 'true':
        return True
    if value.lower() == 'false':
        return False

    # Integer
    if re.match(r'^-?\d+$', value):
        try:
            return int(value)
        except ValueError:
            pass

    # Float
    if re.match(r'^-?\d+\.?\d*(e-?\d+)?$', value):
        try:
            return float(value)
        except ValueError:
            pass

    # String (strip quotes)
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]

    return value


def load_yaml_file(path: str) -> dict:
    """Load YAML from file."""
    with open(path, 'r') as f:
        content = f.read()
    try:
        import yaml
        return yaml.safe_load(content)
    except ImportError:
        pass
    except Exception:
        pass
    return simple_yaml_load(content)


# In-memory storage for meshes
MEShes: dict[str, dict] = {}


class ConditionType(str, Enum):
    HEALTHY = "Healthy"
    PRECHECKS_PASSED = "PrechecksPassed"
    SCALING = "Scaling"
    GRACEFUL_SHUTDOWN = "GracefulShutdown"


def validate_size(size: str) -> bool:
    """Validate memory quantity format: non-negative integer or with Ki/Mi/Gi/Ti suffix."""
    if not size:
        return False
    match = re.match(r'^(\d+)(Ki|Mi|Gi|Ti)?$', size)
    if not match:
        return False
    num = int(match.group(1))
    return num >= 0


def validate_instances(instances: Any) -> list[dict]:
    """Validate spec.instances field."""
    errors = []
    if instances is None:
        return errors
    if not isinstance(instances, int):
        errors.append({
            "field": "spec.instances",
            "type": "invalid",
            "message": f"value '{instances}' must be a non-negative integer",
        })
    elif instances < 0:
        errors.append({
            "field": "spec.instances",
            "type": "invalid",
            "message": f"value '{instances}' must be a non-negative integer",
        })
    return errors


def validate_replication_factor(value: Any, instances: int) -> list[dict]:
    """Validate replication factor against instances."""
    errors = []
    if value is None:
        return errors
    if not isinstance(value, int):
        errors.append({
            "field": "spec.network.replicationFactor",
            "type": "invalid",
            "message": f"value '{value}' must be an integer",
        })
    elif value < 1:
        errors.append({
            "field": "spec.network.replicationFactor",
            "type": "invalid",
            "message": f"value '{value}' must be at least 1",
        })
    elif value > instances:
        errors.append({
            "field": "spec.network.replicationFactor",
            "type": "invalid",
            "message": f"value '{value}' must not exceed spec.instances ({instances})",
        })
    return errors


def validate_storage_size(size: Any) -> list[dict]:
    """Validate storage size format."""
    errors = []
    if size is None:
        return errors
    if not isinstance(size, str):
        errors.append({
            "field": "spec.network.storage.size",
            "type": "invalid",
            "message": f"value '{size}' is not a valid memory quantity",
        })
    elif not validate_size(size):
        errors.append({
            "field": "spec.network.storage.size",
            "type": "invalid",
            "message": f"value '{size}' is not a valid memory quantity",
        })
    return errors


def sort_conditions(conditions: list[dict]) -> list[dict]:
    """Sort conditions by type ascending and ensure uniqueness."""
    # Use a dict to ensure uniqueness (type appears at most once)
    condition_map = {}
    for c in conditions:
        condition_map[c["type"]] = c
    # Sort by type ascending
    return [condition_map[t] for t in sorted(condition_map.keys())]


def get_initial_conditions() -> list[dict]:
    """Get initial conditions for a new mesh."""
    return [
        {"type": "Healthy", "status": "True", "message": ""},
        {"type": "PrechecksPassed", "status": "True", "message": ""},
    ]


def build_storage_output(storage: dict) -> dict:
    """Build storage output based on ephemeral flag."""
    if storage.get("ephemeral", False):
        return {"ephemeral": True}
    result = {"ephemeral": storage.get("ephemeral", False)}
    if "size" in storage:
        result["size"] = storage["size"]
    if "className" in storage:
        result["className"] = storage["className"]
    return result


def deep_merge_dicts(base: dict, update: dict) -> dict:
    """Deep merge update into base."""
    result = base.copy()
    for key, value in update.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge_dicts(result[key], value)
        else:
            result[key] = value
    return result


def create_mesh(data: dict) -> tuple[Optional[dict], list[dict]]:
    """Create a new mesh from YAML data."""
    errors = []

    # Validate data structure
    if data is None or not isinstance(data, dict):
        errors.append({
            "field": "metadata.name",
            "type": "required",
            "message": "metadata.name is required",
        })
        return None, errors

    if "metadata" not in data or not isinstance(data["metadata"], dict) or "name" not in data["metadata"]:
        errors.append({
            "field": "metadata.name",
            "type": "required",
            "message": "metadata.name is required",
        })
        return None, errors

    name = data["metadata"]["name"]

    # Check if mesh already exists
    if name in MEShes:
        errors.append({
            "field": "metadata.name",
            "type": "already_exists",
            "message": f"mesh '{name}' already exists",
        })
        return None, errors

    # Parse spec
    spec = data.get("spec", {}) if isinstance(data.get("spec"), dict) else {}
    instances = spec.get("instances")

    # Validate instances if present
    if instances is not None:
        errors.extend(validate_instances(instances))

    # Handle storage
    network = spec.get("network", {}) if isinstance(spec.get("network"), dict) else {}
    storage = network.get("storage", {}) if isinstance(network.get("storage"), dict) else {}

    if "size" not in storage:
        # Default size to "1Gi" when omitted
        if not isinstance(network, dict):
            network = {}
        if "storage" not in network:
            network["storage"] = {}
        network["storage"]["size"] = "1Gi"
        storage = network["storage"]
        spec["network"] = network

    # Validate storage size
    if "size" in storage:
        errors.extend(validate_storage_size(storage["size"]))

    # Handle replication factor default
    rf = network.get("replicationFactor")
    if rf is None and instances and instances > 0:
        # Compute default based on instance count
        if not isinstance(network, dict):
            network = {}
        network["replicationFactor"] = instances
        rf = instances
        spec["network"] = network

    # Validate replication factor
    if rf is not None:
        instances_count = instances if instances else 0
        errors.extend(validate_replication_factor(rf, instances_count))

    if errors:
        return None, errors

    # Build the mesh
    mesh = {
        "apiVersion": data.get("apiVersion", "mesh.example.com/v1"),
        "kind": data.get("kind", "Mesh"),
        "metadata": {"name": name},
        "spec": {
            "instances": instances,
            "network": {},
        },
        "status": {
            "state": "Running" if (instances and instances > 0) else "Stopped",
            "stable": True,
            "instances": {"ready": 0, "starting": 0, "stopped": 0},
            "conditions": get_initial_conditions(),
        },
    }

    # Copy network settings
    if isinstance(network, dict):
        if "storage" in network:
            mesh["spec"]["network"]["storage"] = network["storage"]
        if "replicationFactor" in network:
            mesh["spec"]["network"]["replicationFactor"] = network["replicationFactor"]

    # Set initial instances status
    if instances and instances > 0:
        mesh["status"]["instances"]["ready"] = instances

    # Store mesh
    MEShes[name] = mesh

    return mesh, []


def list_meshes() -> list[dict]:
    """List mesh summaries."""
    result = []
    for name in sorted(MEShes.keys()):
        mesh = MEShes[name]
        result.append({
            "apiVersion": mesh["apiVersion"],
            "kind": mesh["kind"],
            "metadata": {"name": mesh["metadata"]["name"]},
            "spec": {"instances": mesh["spec"]["instances"]},
            "status": {
                "state": mesh["status"]["state"],
                "stable": mesh["status"]["stable"],
                "instances": mesh["status"]["instances"],
            },
        })
    return result


def describe_mesh(name: str) -> tuple[Optional[dict], list[dict]]:
    """Describe a specific mesh."""
    if name not in MEShes:
        return None, [{
            "field": "metadata.name",
            "type": "not_found",
            "message": f"mesh '{name}' not found",
        }]

    mesh = MEShes[name]

    # Return a copy to avoid mutation issues
    result = {
        "apiVersion": mesh["apiVersion"],
        "kind": mesh["kind"],
        "metadata": mesh["metadata"],
        "spec": {},
        "status": {},
    }

    # Deep copy spec
    spec = {}
    if mesh["spec"]["instances"] is not None:
        spec["instances"] = mesh["spec"]["instances"]
    if "network" in mesh["spec"]:
        spec["network"] = {}
        network = mesh["spec"]["network"]
        if "storage" in network:
            spec["network"]["storage"] = build_storage_output(network["storage"])
        if "replicationFactor" in network:
            spec["network"]["replicationFactor"] = network["replicationFactor"]
    result["spec"] = spec

    # Deep copy status (omitting transient Scaling condition on describe)
    status = {
        "state": mesh["status"]["state"],
        "stable": mesh["status"]["stable"],
        "instances": mesh["status"]["instances"].copy(),
        "conditions": sort_conditions(mesh["status"]["conditions"]),
    }

    # Remove Scaling condition (transient) on describe
    status["conditions"] = [c for c in status["conditions"] if not (c["type"] == "Scaling" and c["status"] == "True")]

    if "desiredInstancesOnResume" in mesh["status"]:
        status["desiredInstancesOnResume"] = mesh["status"]["desiredInstancesOnResume"]

    result["status"] = status

    return result, []


def delete_mesh(name: str) -> tuple[Optional[dict], list[dict]]:
    """Delete a mesh."""
    if name not in MEShes:
        return None, [{
            "field": "metadata.name",
            "type": "not_found",
            "message": f"mesh '{name}' not found",
        }]

    mesh = MEShes.pop(name)
    return mesh, []


def update_mesh(data: dict) -> tuple[Optional[dict], list[dict]]:
    """Apply a partial update to a mesh."""
    errors = []

    # Validate metadata.name
    if data is None or not isinstance(data, dict):
        errors.append({
            "field": "metadata.name",
            "type": "required",
            "message": "metadata.name is required",
        })
        return None, errors

    if "metadata" not in data or not isinstance(data["metadata"], dict) or "name" not in data["metadata"]:
        errors.append({
            "field": "metadata.name",
            "type": "required",
            "message": "metadata.name is required",
        })
        return None, errors

    name = data["metadata"]["name"]

    # Check if mesh exists
    if name not in MEShes:
        errors.append({
            "field": "metadata.name",
            "type": "not_found",
            "message": f"mesh '{name}' not found",
        })
        return None, errors

    mesh = MEShes[name]
    old_instances = mesh["spec"]["instances"] if mesh["spec"]["instances"] else 0

    # Track changes for status updates
    scaling_added = False
    scaling_message = ""
    graceful_shutdown_added = False

    # Process updates field by field (merge rules)
    update_spec = data.get("spec", {}) if isinstance(data.get("spec"), dict) else {}

    # Handle instances update
    if "instances" in update_spec:
        new_instances = update_spec["instances"]

        # Validate
        if not isinstance(new_instances, int):
            errors.append({
                "field": "spec.instances",
                "type": "invalid",
                "message": f"value '{new_instances}' must be a non-negative integer",
            })
        elif new_instances < 0:
            errors.append({
                "field": "spec.instances",
                "type": "invalid",
                "message": f"value '{new_instances}' must be a non-negative integer",
            })

        if not errors:
            # Check for stop (positive to 0)
            if old_instances > 0 and new_instances == 0:
                graceful_shutdown_added = True
                mesh["status"]["state"] = "Stopped"
                mesh["status"]["instances"] = {"ready": 0, "starting": 0, "stopped": old_instances}
                mesh["status"]["desiredInstancesOnResume"] = old_instances
                mesh["status"]["stable"] = False

            # Check for resume (stopped mesh with positive instances)
            elif old_instances == 0 and new_instances > 0 and mesh["status"]["desiredInstancesOnResume"] is not None:
                # Resume case
                del mesh["status"]["desiredInstancesOnResume"]
                scaling_added = True
                scaling_message = "Resuming mesh"
                mesh["status"]["instances"]["ready"] = 0
                mesh["status"]["instances"]["starting"] = new_instances
                mesh["status"]["instances"]["stopped"] = 0
                mesh["status"]["state"] = "Running"
                mesh["status"]["stable"] = False

            # Check for scale up
            elif new_instances > old_instances:
                scaling_added = True
                scaling_message = f"Scaling from {old_instances} to {new_instances} instances"
                mesh["status"]["instances"]["ready"] = old_instances
                mesh["status"]["instances"]["starting"] = new_instances - old_instances
                mesh["status"]["stable"] = False

            # Check for scale down
            elif new_instances < old_instances:
                scaling_added = True
                scaling_message = ""
                mesh["status"]["instances"]["ready"] = new_instances
                mesh["status"]["instances"]["starting"] = 0
                mesh["status"]["instances"]["stopped"] = old_instances - new_instances
                mesh["status"]["stable"] = False

            # Update spec instances
            mesh["spec"]["instances"] = new_instances

    # Handle network updates
    if "network" in update_spec:
        update_network = update_spec["network"]

        # Ensure network exists in spec
        if "network" not in mesh["spec"]:
            mesh["spec"]["network"] = {}

        current_network = mesh["spec"]["network"]

        # Handle storage updates
        if "storage" in update_network:
            update_storage = update_network["storage"]
            current_storage = current_network.get("storage", {})

            # Ensure storage exists
            if "storage" not in current_network:
                current_network["storage"] = {}
                current_storage = current_network["storage"]

            # Check for immutable size changes
            if "size" in update_storage and "size" in current_storage:
                if update_storage["size"] != current_storage["size"]:
                    errors.append({
                        "field": "spec.network.storage.size",
                        "type": "immutable",
                        "message": f"field 'spec.network.storage.size' is immutable after creation",
                    })
            else:
                # Apply updates for other fields
                if "size" in update_storage:
                    current_storage["size"] = update_storage["size"]
                if "ephemeral" in update_storage:
                    current_storage["ephemeral"] = update_storage["ephemeral"]
                if "className" in update_storage:
                    current_storage["className"] = update_storage["className"]

        # Handle replicationFactor updates
        if "replicationFactor" in update_network:
            new_rf = update_network["replicationFactor"]
            current_instances = mesh["spec"]["instances"] if mesh["spec"]["instances"] else 0

            # Validate
            if not isinstance(new_rf, int):
                errors.append({
                    "field": "spec.network.replicationFactor",
                    "type": "invalid",
                    "message": f"value '{new_rf}' must be an integer",
                })
            elif new_rf < 1:
                errors.append({
                    "field": "spec.network.replicationFactor",
                    "type": "invalid",
                    "message": f"value '{new_rf}' must be at least 1",
                })
            elif new_rf > current_instances:
                errors.append({
                    "field": "spec.network.replicationFactor",
                    "type": "invalid",
                    "message": f"value '{new_rf}' must not exceed spec.instances ({current_instances})",
                })
            else:
                current_network["replicationFactor"] = new_rf

    if errors:
        return None, errors

    # Apply transient conditions
    if scaling_added:
        # Remove existing Scaling condition if present
        mesh["status"]["conditions"] = [
            c for c in mesh["status"]["conditions"] if c["type"] != "Scaling"
        ]
        mesh["status"]["conditions"].append({
            "type": "Scaling",
            "status": "True",
            "message": scaling_message,
        })

    if graceful_shutdown_added:
        mesh["status"]["conditions"].append({
            "type": "GracefulShutdown",
            "status": "True",
            "message": "",
        })

    # Return the updated mesh
    return describe_mesh(name)


def create_yaml_file(path: str, content: str) -> None:
    """Create a YAML test file."""
    with open(path, 'w') as f:
        f.write(content)


def main():
    parser = argparse.ArgumentParser(description="Mesh Lifecycle and Topology CLI")
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Mesh commands
    mesh_parser = subparsers.add_parser("mesh", help="Mesh operations")
    mesh_subparsers = mesh_parser.add_subparsers(dest="mesh_command", help="Mesh commands")

    # mesh create
    create_parser = mesh_subparsers.add_parser("create", help="Create a mesh")
    create_parser.add_argument("-f", "--file", required=True, help="YAML file path")

    # mesh list
    mesh_subparsers.add_parser("list", help="List meshes")

    # mesh describe
    describe_parser = mesh_subparsers.add_parser("describe", help="Describe a mesh")
    describe_parser.add_argument("name", help="Mesh name")

    # mesh delete
    delete_parser = mesh_subparsers.add_parser("delete", help="Delete a mesh")
    delete_parser.add_argument("name", help="Mesh name")

    # mesh update
    update_parser = mesh_subparsers.add_parser("update", help="Update a mesh")
    update_parser.add_argument("-f", "--file", required=True, help="YAML file path")

    args = parser.parse_args()

    if args.command != "mesh" or args.mesh_command is None:
        parser.print_help()
        sys.exit(1)

    try:
        if args.mesh_command == "create":
            data = load_yaml_file(args.file)
            result, errors = create_mesh(data)
            if errors:
                print(json.dumps({"errors": errors}, indent=2))
                sys.exit(1)
            print(json.dumps(result, indent=2))

        elif args.mesh_command == "list":
            result = list_meshes()
            print(json.dumps(result, indent=2))

        elif args.mesh_command == "describe":
            result, errors = describe_mesh(args.name)
            if errors:
                print(json.dumps({"errors": errors}, indent=2))
                sys.exit(1)
            print(json.dumps(result, indent=2))

        elif args.mesh_command == "delete":
            result, errors = delete_mesh(args.name)
            if errors:
                print(json.dumps({"errors": errors}, indent=2))
                sys.exit(1)
            print(json.dumps(result, indent=2))

        elif args.mesh_command == "update":
            data = load_yaml_file(args.file)
            result, errors = update_mesh(data)
            if errors:
                print(json.dumps({"errors": errors}, indent=2))
                sys.exit(1)
            print(json.dumps(result, indent=2))

    except FileNotFoundError as e:
        print(json.dumps({"errors": [{
            "field": "",
            "type": "parse",
            "message": f"file not found: {e}",
        }]}, indent=2))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({"errors": [{
            "field": "",
            "type": "parse",
            "message": f"failed to parse YAML: {e}",
        }]}, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
