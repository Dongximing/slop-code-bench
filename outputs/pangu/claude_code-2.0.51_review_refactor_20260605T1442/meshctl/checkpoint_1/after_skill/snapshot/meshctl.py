#!/usr/bin/env python3
"""meshctl - Manage mesh resources from YAML specs."""

import argparse
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

import yaml

# Storage file for mesh resources
STORAGE_FILE = "/tmp/meshes.json"


def load_storage() -> Dict[str, Dict]:
    """Load all stored mesh resources."""
    if os.path.exists(STORAGE_FILE):
        try:
            with open(STORAGE_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def save_storage(data: Dict[str, Dict]) -> None:
    """Save all mesh resources to storage."""
    with open(STORAGE_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def parse_memory_quantity(value: str) -> Tuple[bool, int, str]:
    """Parse memory quantity and return (valid, bytes, error_message)."""
    if not isinstance(value, str):
        return False, 0, "memory quantity must be a string"

    # Match patterns: number, numberKi, numberMi, numberGi, numberTi
    match = re.match(r'^(\d+)(Ki|Mi|Gi|Ti)?$', value)
    if not match:
        return False, 0, f"invalid memory quantity: {value}"

    num = int(match.group(1))
    unit = match.group(2)

    if unit is None:
        # Plain number means bytes
        return True, num, ""
    elif unit == "Ki":
        return True, num * 1024, ""
    elif unit == "Mi":
        return True, num * 1024 * 1024, ""
    elif unit == "Gi":
        return True, num * 1024 * 1024 * 1024, ""
    elif unit == "Ti":
        return True, num * 1024 * 1024 * 1024 * 1024, ""
    return False, 0, f"invalid memory quantity: {value}"


def parse_cpu_quantity(value: str) -> Tuple[bool, int, str]:
    """Parse CPU quantity and return (valid, millicores, error_message)."""
    if not isinstance(value, str):
        return False, 0, "cpu quantity must be a string"

    # Match patterns: number, numberm
    match = re.match(r'^(\d+)(m)?$', value)
    if not match:
        return False, 0, f"invalid cpu quantity: {value}"

    num = int(match.group(1))
    unit = match.group(2)

    if unit is None:
        # Plain number means cores
        return True, num * 1000, ""
    elif unit == "m":
        return True, num, ""
    return False, 0, f"invalid cpu quantity: {value}"


def validate_name(name: Any) -> List[Dict]:
    """Validate metadata.name field."""
    errors = []

    if name is None or name == "":
        errors.append({
            "field": "metadata.name",
            "message": "metadata.name is required and cannot be empty",
            "type": "required"
        })
        return errors

    if not isinstance(name, str):
        errors.append({
            "field": "metadata.name",
            "message": "metadata.name must be a string",
            "type": "invalid"
        })
        return errors

    # Pattern: ^[a-z0-9][a-z0-9-]*[a-z0-9]$
    # Minimum length 2
    if len(name) < 2:
        errors.append({
            "field": "metadata.name",
            "message": "metadata.name must be at least 2 characters long",
            "type": "invalid"
        })
    elif not re.match(r'^[a-z0-9][a-z0-9-]*[a-z0-9]$', name):
        errors.append({
            "field": "metadata.name",
            "message": "metadata.name must match pattern ^[a-z0-9][a-z0-9-]*[a-z0-9]$",
            "type": "invalid"
        })

    return errors


def validate_runtime(runtime: Any) -> List[Dict]:
    """Validate spec.runtime field."""
    errors = []

    if runtime is None:
        return errors

    if not isinstance(runtime, str):
        errors.append({
            "field": "spec.runtime",
            "message": "spec.runtime must be a string",
            "type": "invalid"
        })
        return errors

    # Must parse as major.minor.patch
    parts = runtime.split('.')
    if len(parts) != 3:
        errors.append({
            "field": "spec.runtime",
            "message": f"spec.runtime must be in major.minor.patch format, got: {runtime}",
            "type": "invalid"
        })
        return errors

    for i, part in enumerate(parts):
        if not part.isdigit():
            errors.append({
                "field": "spec.runtime",
                "message": f"spec.runtime parts must be non-negative integers, got: {runtime}",
                "type": "invalid"
            })
            return errors

    return errors


def validate_instances(instances: Any) -> List[Dict]:
    """Validate spec.instances field."""
    errors = []

    if instances is None:
        return errors

    if not isinstance(instances, int):
        errors.append({
            "field": "spec.instances",
            "message": "spec.instances must be an integer",
            "type": "invalid"
        })
        return errors

    if instances <= 0:
        errors.append({
            "field": "spec.instances",
            "message": "spec.instances must be a positive integer",
            "type": "invalid"
        })

    return errors


def validate_resources(resources: Any) -> List[Dict]:
    """Validate spec.resources field."""
    errors = []

    if resources is None:
        return errors

    if not isinstance(resources, dict):
        errors.append({
            "field": "spec.resources",
            "message": "spec.resources must be an object",
            "type": "invalid"
        })
        return errors

    # Validate memory
    memory = resources.get("memory")
    if memory is not None:
        if not isinstance(memory, dict):
            errors.append({
                "field": "spec.resources.memory",
                "message": "spec.resources.memory must be an object",
                "type": "invalid"
            })
        else:
            # Check limit
            limit = memory.get("limit")
            if limit is None:
                errors.append({
                    "field": "spec.resources.memory.limit",
                    "message": "spec.resources.memory.limit is required when spec.resources.memory is present",
                    "type": "required"
                })
            else:
                valid, _, msg = parse_memory_quantity(limit)
                if not valid:
                    errors.append({
                        "field": "spec.resources.memory.limit",
                        "message": msg,
                        "type": "invalid"
                    })

            # Check request
            request = memory.get("request")
            if request is not None:
                valid, _, msg = parse_memory_quantity(request)
                if not valid:
                    errors.append({
                        "field": "spec.resources.memory.request",
                        "message": msg,
                        "type": "invalid"
                    })
                elif limit is not None:
                    # Check request <= limit
                    req_valid, req_bytes, _ = parse_memory_quantity(request)
                    lim_valid, lim_bytes, _ = parse_memory_quantity(limit)
                    if req_valid and lim_valid and req_bytes > lim_bytes:
                        errors.append({
                            "field": "spec.resources.memory.request",
                            "message": "spec.resources.memory.request cannot exceed limit",
                            "type": "invalid"
                        })

    # Validate cpu
    cpu = resources.get("cpu")
    if cpu is not None:
        if not isinstance(cpu, dict):
            errors.append({
                "field": "spec.resources.cpu",
                "message": "spec.resources.cpu must be an object",
                "type": "invalid"
            })
        else:
            # Check limit
            limit = cpu.get("limit")
            if limit is None:
                errors.append({
                    "field": "spec.resources.cpu.limit",
                    "message": "spec.resources.cpu.limit is required when spec.resources.cpu is present",
                    "type": "required"
                })
            else:
                valid, _, msg = parse_cpu_quantity(limit)
                if not valid:
                    errors.append({
                        "field": "spec.resources.cpu.limit",
                        "message": msg,
                        "type": "invalid"
                    })

            # Check request
            request = cpu.get("request")
            if request is not None:
                valid, _, msg = parse_cpu_quantity(request)
                if not valid:
                    errors.append({
                        "field": "spec.resources.cpu.request",
                        "message": msg,
                        "type": "invalid"
                    })
                elif limit is not None:
                    # Check request <= limit
                    req_valid, req_millicores, _ = parse_cpu_quantity(request)
                    lim_valid, lim_millicores, _ = parse_cpu_quantity(limit)
                    if req_valid and lim_valid and req_millicores > lim_millicores:
                        errors.append({
                            "field": "spec.resources.cpu.request",
                            "message": "spec.resources.cpu.request cannot exceed limit",
                            "type": "invalid"
                        })

    return errors


def validate_access(access: Any) -> List[Dict]:
    """Validate spec.access field."""
    errors = []

    if access is None:
        return errors

    if not isinstance(access, dict):
        errors.append({
            "field": "spec.access",
            "message": "spec.access must be an object",
            "type": "invalid"
        })
        return errors

    # Validate authentication.enabled
    auth = access.get("authentication")
    if auth is not None:
        if not isinstance(auth, dict):
            errors.append({
                "field": "spec.access.authentication",
                "message": "spec.access.authentication must be an object",
                "type": "invalid"
            })
        elif "enabled" in auth:
            if not isinstance(auth["enabled"], bool):
                errors.append({
                    "field": "spec.access.authentication.enabled",
                    "message": "spec.access.authentication.enabled must be a boolean",
                    "type": "invalid"
                })

    return errors


def validate_migration(migration: Any) -> List[Dict]:
    """Validate spec.migration field."""
    errors = []

    if migration is None:
        return errors

    if not isinstance(migration, dict):
        errors.append({
            "field": "spec.migration",
            "message": "spec.migration must be an object",
            "type": "invalid"
        })
        return errors

    strategy = migration.get("strategy")
    if strategy is not None:
        if not isinstance(strategy, str):
            errors.append({
                "field": "spec.migration.strategy",
                "message": "spec.migration.strategy must be a string",
                "type": "invalid"
            })
        elif strategy != "FullStop":
            errors.append({
                "field": "spec.migration.strategy",
                "message": f"spec.migration.strategy must be 'FullStop', got: {strategy}",
                "type": "invalid"
            })

    return errors


def check_forbidden_auto_scaling(spec: Any, path: str = "spec") -> List[Dict]:
    """Recursively check for forbidden autoScaling field under spec."""
    errors = []

    if spec is None or not isinstance(spec, dict):
        return errors

    for key, value in spec.items():
        full_path = f"{path}.{key}" if path else key

        if key == "autoScaling":
            errors.append({
                "field": full_path,
                "message": f"field {full_path} is forbidden",
                "type": "forbidden"
            })
        elif isinstance(value, dict):
            errors.extend(check_forbidden_auto_scaling(value, full_path))

    return errors


def validate_yaml(data: Any, check_duplicate: bool = True) -> Tuple[List[Dict], Optional[Dict]]:
    """Validate the entire YAML document and apply defaults.

    Returns tuple of (errors, full_resource_with_defaults).
    """
    errors = []

    # Check root is a mapping
    if not isinstance(data, dict):
        errors.append({
            "field": "",
            "message": "YAML document must be a mapping",
            "type": "parse"
        })
        return errors, None

    # Check for forbidden fields at top level
    if "autoScaling" in data:
        errors.append({
            "field": "autoScaling",
            "message": "field autoScaling is forbidden",
            "type": "forbidden"
        })

    # Extract metadata and spec
    metadata = data.get("metadata")
    spec = data.get("spec")

    # Validate metadata.name
    if metadata is None:
        errors.append({
            "field": "metadata",
            "message": "metadata is required",
            "type": "required"
        })
    elif not isinstance(metadata, dict):
        errors.append({
            "field": "metadata",
            "message": "metadata must be an object",
            "type": "invalid"
        })
    else:
        errors.extend(validate_name(metadata.get("name")))

    # Validate spec and its subfields
    if spec is None:
        errors.append({
            "field": "spec",
            "message": "spec is required",
            "type": "required"
        })
    elif not isinstance(spec, dict):
        errors.append({
            "field": "spec",
            "message": "spec must be an object",
            "type": "invalid"
        })
    else:
        # Check for forbidden autoScaling
        errors.extend(check_forbidden_auto_scaling(spec))

        # Validate spec fields
        errors.extend(validate_instances(spec.get("instances")))
        errors.extend(validate_runtime(spec.get("runtime")))
        errors.extend(validate_resources(spec.get("resources")))
        errors.extend(validate_access(spec.get("access")))
        errors.extend(validate_migration(spec.get("migration")))

    # If there are errors, return early
    if errors:
        return errors, None

    # Build full resource with defaults
    name = metadata["name"]

    # Apply defaults to spec
    full_spec = {}

    # instances: default 1
    full_spec["instances"] = spec.get("instances", 1)

    # runtime: optional, no default
    if "runtime" in spec:
        full_spec["runtime"] = spec["runtime"]

    # resources
    resources = spec.get("resources")
    if resources is not None:
        full_resources = {}

        # memory
        memory = resources.get("memory")
        if memory is not None:
            full_memory = {}
            limit = memory["limit"]
            full_memory["limit"] = limit
            # request defaults to limit if not provided
            if "request" in memory:
                full_memory["request"] = memory["request"]
            else:
                full_memory["request"] = limit
            full_resources["memory"] = full_memory
        else:
            # Default memory
            full_resources["memory"] = {"limit": "1Gi", "request": "1Gi"}

        # cpu
        cpu = resources.get("cpu")
        if cpu is not None and "limit" in cpu:
            full_cpu = {}
            full_cpu["limit"] = cpu["limit"]
            if "request" in cpu:
                full_cpu["request"] = cpu["request"]
            else:
                full_cpu["request"] = cpu["limit"]
            full_resources["cpu"] = full_cpu

        if full_resources:
            full_spec["resources"] = full_resources

    # access
    access = spec.get("access")
    if access is not None:
        full_access = {}
        auth = access.get("authentication")
        if auth is not None:
            full_auth = {}
            # enabled defaults to True
            if "enabled" in auth:
                full_auth["enabled"] = auth["enabled"]
            else:
                full_auth["enabled"] = True
            full_access["authentication"] = full_auth
        if full_access:
            full_spec["access"] = full_access

    # migration: default "FullStop"
    if "migration" in spec:
        full_spec["migration"] = spec["migration"]
    else:
        full_spec["migration"] = {"strategy": "FullStop"}

    full_resource = {
        "metadata": {"name": name},
        "spec": full_spec,
        "status": {"state": "Running"}
    }

    # Check for duplicate if validating for create
    if check_duplicate:
        storage = load_storage()
        if name in storage:
            errors.append({
                "field": "metadata.name",
                "message": f"mesh with name '{name}' already exists",
                "type": "duplicate"
            })
            return errors, None

    return errors, full_resource


def cmd_create(file_path: str) -> None:
    """Handle mesh create command."""
    # Read and parse YAML
    try:
        with open(file_path, 'r') as f:
            data = yaml.safe_load(f)
    except IOError as e:
        print(json.dumps({
            "errors": [{
                "field": "",
                "message": f"cannot read file: {e}",
                "type": "parse"
            }]
        }))
        return
    except yaml.YAMLError as e:
        print(json.dumps({
            "errors": [{
                "field": "",
                "message": f"cannot parse YAML: {e}",
                "type": "parse"
            }]
        }))
        return

    # Validate and apply defaults
    errors, resource = validate_yaml(data, check_duplicate=True)

    if errors:
        print(json.dumps({"errors": errors}))
        return

    # Persist resource
    storage = load_storage()
    name = resource["metadata"]["name"]
    storage[name] = resource
    save_storage(storage)

    # Output full resource
    print(json.dumps(resource))


def cmd_list() -> None:
    """Handle mesh list command."""
    storage = load_storage()

    # Build list of summaries sorted by name ascending, case-sensitive
    summaries = []
    for name, resource in sorted(storage.items()):
        summaries.append({
            "name": name,
            "status": resource.get("status", {"state": "Unknown"})
        })

    print(json.dumps(summaries))


def cmd_describe(name: str) -> None:
    """Handle mesh describe command."""
    storage = load_storage()

    if name not in storage:
        print(json.dumps({
            "errors": [{
                "field": "metadata.name",
                "message": f"mesh '{name}' not found",
                "type": "not_found"
            }]
        }))
        return

    print(json.dumps(storage[name]))


def cmd_delete(name: str) -> None:
    """Handle mesh delete command."""
    storage = load_storage()

    if name not in storage:
        print(json.dumps({
            "errors": [{
                "field": "metadata.name",
                "message": f"mesh '{name}' not found",
                "type": "not_found"
            }]
        }))
        return

    # Delete and confirm
    del storage[name]
    save_storage(storage)

    print(json.dumps({
        "message": f"mesh '{name}' deleted successfully",
        "metadata": {"name": name}
    }))


def main():
    parser = argparse.ArgumentParser(
        prog="meshctl",
        description="Manage mesh resources from YAML specs"
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # mesh command
    mesh_parser = subparsers.add_parser("mesh", help="Manage mesh resources")
    mesh_subparsers = mesh_parser.add_subparsers(dest="mesh_operation", required=True)

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
        if args.mesh_operation == "create":
            cmd_create(args.file)
        elif args.mesh_operation == "list":
            cmd_list()
        elif args.mesh_operation == "describe":
            cmd_describe(args.name)
        elif args.mesh_operation == "delete":
            cmd_delete(args.name)


if __name__ == "__main__":
    main()
