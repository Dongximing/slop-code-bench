#!/usr/bin/env python3
"""meshctl - Manage mesh resources from YAML specs"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

import yaml

DATA_DIR = Path(".mesh")
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")
RUNTIME_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")


class ValidationError:
    """Represents a validation error"""

    def __init__(self, field: str, message: str, error_type: str):
        self.field = field
        self.message = message
        self.type = error_type

    def to_dict(self):
        return {
            "field": self.field,
            "message": self.message,
            "type": self.type,
        }


def parse_resource_quantities(value: str, is_cpu: bool = False) -> tuple[int | float, str]:
    """
    Parse memory or CPU quantity.
    Returns (value, unit) where unit is '' for plain numbers, 'Ki','Mi','Gi','Ti' for memory, 'm' for CPU.
    For plain numbers, value is bytes for memory, whole cores for CPU.
    """
    if not isinstance(value, str):
        return (int(value), "")

    match = re.match(r"^(\d+)\s*(Ki|Mi|Gi|Ti|m)?$", value.strip())
    if not match:
        raise ValueError(f"Invalid quantity format: {value}")

    num = int(match.group(1))
    unit = match.group(2) if match.group(2) else ""
    return (num, unit)


def compare_quantities(a: str, b: str, is_cpu: bool = False) -> int:
    """
    Compare two resource quantities.
    Returns -1 if a < b, 0 if a == b, 1 if a > b.
    """
    def to_milli_units(quantity: str) -> int:
        try:
            val, unit = parse_resource_quantities(quantity)
            if unit == "Ki":
                return val * 1024
            elif unit == "Mi":
                return val * 1024 * 1024
            elif unit == "Gi":
                return val * 1024 * 1024 * 1024
            elif unit == "Ti":
                return val * 1024 * 1024 * 1024 * 1024
            elif unit == "m":
                return val  # CPU millicores
            elif unit == "":
                if is_cpu:
                    return val * 1000  # plain integer CPU is whole cores
                else:
                    return val  # plain bytes for memory
            else:
                return val
        except (ValueError, AttributeError):
            return 0

    a_bytes = to_milli_units(a)
    b_bytes = to_milli_units(b)

    if a_bytes < b_bytes:
        return -1
    elif a_bytes > b_bytes:
        return 1
    return 0


def validate_name(name: str, errors: list[ValidationError]):
    """Validate metadata.name field"""
    if name is None or name == "":
        errors.append(ValidationError("metadata.name", "Name is required", "required"))
    elif not isinstance(name, str):
        errors.append(ValidationError("metadata.name", "Name must be a string", "required"))
    elif not NAME_PATTERN.match(name):
        errors.append(ValidationError("metadata.name", "Name must match pattern ^[a-z0-9][a-z0-9-]*[a-z0-9]$", "invalid"))
    elif len(name) < 2:
        errors.append(ValidationError("metadata.name", "Name must be at least 2 characters long", "invalid"))


def validate_instances(spec: dict, errors: list[ValidationError]):
    """Validate spec.instances field"""
    instances = spec.get("instances")
    if instances is None:
        return  # default value will be applied

    if not isinstance(instances, int) or instances <= 0:
        errors.append(ValidationError("spec.instances", "Instances must be a positive integer", "invalid"))


def validate_runtime(spec: dict, errors: list[ValidationError]):
    """Validate spec.runtime field"""
    runtime = spec.get("runtime")
    if runtime is None:
        return

    if not isinstance(runtime, str):
        errors.append(ValidationError("spec.runtime", "Runtime must be a string", "invalid"))
        return

    if not RUNTIME_PATTERN.match(runtime):
        errors.append(ValidationError("spec.runtime", "Runtime must be in major.minor.patch format", "invalid"))
        return

    parts = runtime.split(".")
    try:
        for part in parts:
            val = int(part)
            if val < 0:
                errors.append(ValidationError("spec.runtime", "Runtime parts must be non-negative integers", "invalid"))
                return
    except ValueError:
        errors.append(ValidationError("spec.runtime", "Runtime must be in major.minor.patch format", "invalid"))


def validate_resources(spec: dict, errors: list[ValidationError]):
    """Validate spec.resources fields"""
    resources = spec.get("resources")
    if resources is None:
        return

    if not isinstance(resources, dict):
        errors.append(ValidationError("spec.resources", "Resources must be an object", "invalid"))
        return

    # Validate memory
    memory = resources.get("memory")
    if memory is not None:
        if not isinstance(memory, dict):
            errors.append(ValidationError("spec.resources.memory", "Memory must be an object", "invalid"))
        else:
            if memory.get("limit") is None:
                errors.append(ValidationError("spec.resources.memory.limit", "Memory limit is required", "required"))
            elif not isinstance(memory.get("limit"), str) and not isinstance(memory.get("limit"), int):
                errors.append(ValidationError("spec.resources.memory.limit", "Memory limit must be a string or integer", "invalid"))
            else:
                try:
                    parse_resource_quantities(str(memory.get("limit")))
                except ValueError as e:
                    errors.append(ValidationError("spec.resources.memory.limit", f"Invalid memory quantity: {e}", "invalid"))

            request = memory.get("request")
            limit = memory.get("limit")
            if request is not None and limit is not None:
                try:
                    if compare_quantities(str(request), str(limit), is_cpu=False) > 0:
                        errors.append(ValidationError("spec.resources.memory.request", "Request cannot exceed limit", "invalid"))
                except (ValueError, AttributeError):
                    pass

    # Validate cpu
    cpu = resources.get("cpu")
    if cpu is not None:
        if not isinstance(cpu, dict):
            errors.append(ValidationError("spec.resources.cpu", "CPU must be an object", "invalid"))
        else:
            if cpu.get("limit") is None:
                errors.append(ValidationError("spec.resources.cpu.limit", "CPU limit is required", "required"))
            elif not isinstance(cpu.get("limit"), str) and not isinstance(cpu.get("limit"), int):
                errors.append(ValidationError("spec.resources.cpu.limit", "CPU limit must be a string or integer", "invalid"))
            else:
                try:
                    parse_resource_quantities(str(cpu.get("limit")))
                except ValueError as e:
                    errors.append(ValidationError("spec.resources.cpu.limit", f"Invalid CPU quantity: {e}", "invalid"))

            request = cpu.get("request")
            limit = cpu.get("limit")
            if request is not None and limit is not None:
                try:
                    if compare_quantities(str(request), str(limit), is_cpu=True) > 0:
                        errors.append(ValidationError("spec.resources.cpu.request", "Request cannot exceed limit", "invalid"))
                except (ValueError, AttributeError):
                    pass


def validate_access(spec: dict, errors: list[ValidationError]):
    """Validate spec.access.authentication.enabled field"""
    access = spec.get("access")
    if access is None:
        return

    if not isinstance(access, dict):
        errors.append(ValidationError("spec.access", "Access must be an object", "invalid"))
        return

    auth = access.get("authentication")
    if auth is not None:
        if not isinstance(auth, dict):
            errors.append(ValidationError("spec.access.authentication", "Authentication must be an object", "invalid"))
        elif not isinstance(auth.get("enabled"), bool) and auth.get("enabled") is not None:
            errors.append(ValidationError("spec.access.authentication.enabled", "Enabled must be a boolean", "invalid"))


def validate_migration(spec: dict, errors: list[ValidationError]):
    """Validate spec.migration.strategy field"""
    migration = spec.get("migration")
    if migration is None:
        return

    if not isinstance(migration, dict):
        errors.append(ValidationError("spec.migration", "Migration must be an object", "invalid"))
        return

    strategy = migration.get("strategy")
    if strategy is not None:
        if strategy != "FullStop":
            errors.append(ValidationError("spec.migration.strategy", "Strategy must be 'FullStop'", "invalid"))


def check_forbidden_fields(obj: dict, path: str, errors: list[ValidationError]):
    """Recursively check for forbidden autoScaling fields"""
    if not isinstance(obj, dict):
        return

    for key, value in obj.items():
        current_path = f"{path}.{key}" if path else key
        if key == "autoScaling":
            errors.append(ValidationError(current_path, "autoScaling field is forbidden", "forbidden"))
        else:
            check_forbidden_fields(value, current_path, errors)


def validate_spec(spec: dict) -> list[ValidationError]:
    """Validate the entire spec and return a list of errors"""
    errors = []

    if not isinstance(spec, dict):
        errors.append(ValidationError("spec", "Spec must be an object", "invalid"))
        return errors

    validate_instances(spec, errors)
    validate_runtime(spec, errors)
    validate_resources(spec, errors)
    validate_access(spec, errors)
    validate_migration(spec, errors)
    check_forbidden_fields(spec, "spec", errors)

    return errors


def validate_resource(data: dict, check_duplicate: bool = True) -> list[ValidationError]:
    """Validate a complete resource document"""
    errors = []

    if not isinstance(data, dict):
        errors.append(ValidationError("", "Document must be a mapping", "parse"))
        return errors

    # Validate metadata
    metadata = data.get("metadata")
    if metadata is None:
        errors.append(ValidationError("metadata", "Metadata is required", "required"))
    elif not isinstance(metadata, dict):
        errors.append(ValidationError("metadata", "Metadata must be an object", "required"))
    else:
        name = metadata.get("name")
        validate_name(name, errors)

    # Validate spec
    spec = data.get("spec")
    errors.extend(validate_spec(spec))

    # Check for duplicate name
    if check_duplicate and not errors:
        name = data.get("metadata", {}).get("name")
        if name and resource_exists(name):
            errors.append(ValidationError("metadata.name", f"Mesh '{name}' already exists", "duplicate"))

    return errors


def ensure_data_dir():
    """Ensure the data directory exists"""
    DATA_DIR.mkdir(exist_ok=True)


def get_resource_path(name: str) -> Path:
    """Get the file path for a resource by name"""
    return DATA_DIR / f"{name}.json"


def resource_exists(name: str) -> bool:
    """Check if a resource with the given name exists"""
    return get_resource_path(name).exists()


def load_resource(name: str) -> dict | None:
    """Load a resource by name"""
    path = get_resource_path(name)
    if not path.exists():
        return None
    with open(path, "r") as f:
        return json.load(f)


def save_resource(name: str, data: dict):
    """Save a resource to disk"""
    ensure_data_dir()
    path = get_resource_path(name)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def delete_resource(name: str) -> bool:
    """Delete a resource by name"""
    path = get_resource_path(name)
    if not path.exists():
        return False
    path.unlink()
    return True


def list_all_resources() -> list[dict]:
    """List all resources"""
    ensure_data_dir()
    resources = []
    for path in DATA_DIR.glob("*.json"):
        try:
            with open(path, "r") as f:
                resource = json.load(f)
                resources.append(resource)
        except (json.JSONDecodeError, IOError):
            continue
    return resources


def apply_defaults(data: dict) -> dict:
    """Apply all defaults to a resource specification"""
    spec = data.get("spec", {})

    # Default for instances
    if "instances" not in spec:
        spec["instances"] = 1

    # Default for resources.memory
    resources = spec.get("resources", {})
    if "memory" not in resources:
        resources["memory"] = {"limit": "1Gi", "request": "1Gi"}
    elif resources["memory"] is not None:
        memory = resources["memory"]
        if "request" not in memory and "limit" in memory:
            memory["request"] = memory["limit"]

    # Default for resources.cpu - only apply if cpu key is present
    if "cpu" in resources and resources["cpu"] is not None:
        cpu = resources["cpu"]
        if "request" not in cpu and "limit" in cpu:
            cpu["request"] = cpu["limit"]

    # Default for access.authentication.enabled
    access = spec.get("access", {})
    if "authentication" not in access:
        access["authentication"] = {"enabled": True}
    elif access["authentication"] is not None:
        auth = access["authentication"]
        if "enabled" not in auth:
            auth["enabled"] = True

    # Default for migration.strategy
    migration = spec.get("migration", {})
    if "strategy" not in migration:
        migration["strategy"] = "FullStop"

    # Only set resources if memory was defaulted or cpu/key exists
    if "memory" in resources or "cpu" in resources:
        spec["resources"] = resources
    spec["access"] = access
    spec["migration"] = migration
    data["spec"] = spec

    return data


def create_resource(data: dict) -> dict:
    """Create and persist a new resource"""
    name = data["metadata"]["name"]
    data = apply_defaults(data)
    data["status"] = {"state": "Running"}
    save_resource(name, data)
    return data


def list_resources() -> list[dict]:
    """List all resources as summaries"""
    resources = list_all_resources()
    summaries = []
    for r in resources:
        summaries.append({
            "name": r["metadata"]["name"],
            "status": r.get("status", {"state": "Unknown"})
        })
    summaries.sort(key=lambda x: x["name"])
    return summaries


def describe_resource(name: str) -> dict | None:
    """Get a resource by name"""
    return load_resource(name)


def delete_resource_and_get_confirmation(name: str) -> dict:
    """Delete a resource and return confirmation"""
    delete_resource(name)
    return {
        "message": f"Mesh '{name}' deleted",
        "metadata": {"name": name}
    }


def output_errors(errors: list[ValidationError]):
    """Output errors as JSON"""
    result = {"errors": [e.to_dict() for e in errors]}
    print(json.dumps(result))


def output_json(data: dict | list):
    """Output data as JSON"""
    print(json.dumps(data))


def read_yaml_file(path: str) -> tuple[dict | None, list[ValidationError]]:
    """Read and parse a YAML file"""
    try:
        with open(path, "r") as f:
            content = f.read()
            if not content.strip():
                return None, [ValidationError("", "File is empty", "parse")]
            data = yaml.safe_load(content)
            if data is None:
                return None, [ValidationError("", "File is empty or contains only whitespace", "parse")]
            return data, []
    except FileNotFoundError:
        return None, [ValidationError("", f"File not found: {path}", "parse")]
    except yaml.YAMLError as e:
        return None, [ValidationError("", f"YAML parse error: {e}", "parse")]
    except IOError as e:
        return None, [ValidationError("", f"Error reading file: {e}", "parse")]


def cmd_create_yaml(yaml_path: str):
    """Handle mesh create -f <path> command"""
    data, parse_errors = read_yaml_file(yaml_path)
    if parse_errors:
        output_errors(parse_errors)
        return

    # Validate the resource
    errors = validate_resource(data, check_duplicate=True)
    if errors:
        output_errors(errors)
        return

    # Create and persist
    result = create_resource(data)
    output_json(result)


def cmd_list():
    """Handle mesh list command"""
    resources = list_resources()
    output_json(resources)


def cmd_describe(name: str):
    """Handle mesh describe <name> command"""
    if not resource_exists(name):
        errors = [ValidationError("metadata.name", f"Mesh '{name}' not found", "not_found")]
        output_errors(errors)
        return

    resource = describe_resource(name)
    output_json(resource)


def cmd_delete(name: str):
    """Handle mesh delete <name> command"""
    if not resource_exists(name):
        errors = [ValidationError("metadata.name", f"Mesh '{name}' not found", "not_found")]
        output_errors(errors)
        return

    confirmation = delete_resource_and_get_confirmation(name)
    output_json(confirmation)


def main():
    parser = argparse.ArgumentParser(prog="meshctl.py", add_help=False)
    parser.add_argument("mesh", nargs="?")
    parser.add_argument("operation", nargs="?")

    # Parse first two args to determine command
    args, remaining = parser.parse_known_args()

    if args.mesh != "mesh":
        print("Usage: python meshctl.py mesh <operation> [arguments]")
        sys.exit(1)

    operation = args.operation

    if operation == "create":
        # Parse create-specific args
        create_parser = argparse.ArgumentParser(prog="meshctl.py mesh create")
        create_parser.add_argument("-f", "--file", required=True, help="YAML file path")
        create_args, _ = create_parser.parse_known_args(remaining)
        cmd_create_yaml(create_args.file)

    elif operation == "list":
        # Parse list args (none expected)
        list_parser = argparse.ArgumentParser(prog="meshctl.py mesh list")
        list_parser.parse_known_args(remaining)
        cmd_list()

    elif operation == "describe":
        # Parse describe args
        describe_parser = argparse.ArgumentParser(prog="meshctl.py mesh describe")
        describe_parser.add_argument("name", help="Mesh name")
        describe_args, _ = describe_parser.parse_known_args(remaining)
        cmd_describe(describe_args.name)

    elif operation == "delete":
        # Parse delete args
        delete_parser = argparse.ArgumentParser(prog="meshctl.py mesh delete")
        delete_parser.add_argument("name", help="Mesh name")
        delete_args, _ = delete_parser.parse_known_args(remaining)
        cmd_delete(delete_args.name)

    else:
        print("Usage: python meshctl.py mesh <operation> [arguments]")
        print("\nOperations:")
        print("  create    Create a mesh from a YAML file")
        print("  list      List all meshes")
        print("  describe  Describe a mesh")
        print("  delete    Delete a mesh")
        sys.exit(1)


if __name__ == "__main__":
    main()
