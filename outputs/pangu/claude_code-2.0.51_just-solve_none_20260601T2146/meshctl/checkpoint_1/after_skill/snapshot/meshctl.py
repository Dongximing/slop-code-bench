#!/usr/bin/env python3
"""meshctl - Manage mesh resources from YAML specs"""

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

DATA_DIR = Path(".mesh")
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")
RUNTIME_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")


def e(field: str, msg: str, etype: str):
    """Create an error dict"""
    return {"field": field, "message": msg, "type": etype}


def parse_quantity(value: str):
    """Parse memory or CPU quantity. Returns (value, unit)"""
    if not isinstance(value, str):
        return (int(value), "")
    match = re.match(r"^(\d+)\s*(Ki|Mi|Gi|Ti|m)?$", value.strip())
    if not match:
        raise ValueError(f"Invalid quantity format: {value}")
    return (int(match.group(1)), match.group(2) or "")


def to_bytes(quantity: str) -> int:
    """Convert resource quantity to bytes/millicores"""
    val, unit = parse_quantity(quantity)
    multipliers = {"Ki": 1024, "Mi": 1024 * 1024, "Gi": 1024 * 1024 * 1024, "Ti": 1024 * 1024 * 1024 * 1024, "m": 1}
    if unit in multipliers:
        return val * multipliers[unit]
    return val  # plain bytes for memory, whole cores for CPU


def validate_name(name):
    """Validate metadata.name field. Returns list of error dicts."""
    if not name:
        return [e("metadata.name", "Name is required", "required")]
    if not isinstance(name, str):
        return [e("metadata.name", "Name must be a string", "required")]
    if not NAME_PATTERN.match(name):
        return [e("metadata.name", "Name must match pattern ^[a-z0-9][a-z0-9-]*[a-z0-9]$", "invalid")]
    if len(name) < 2:
        return [e("metadata.name", "Name must be at least 2 characters long", "invalid")]
    return []


def validate_spec(spec):
    """Validate the entire spec and return a list of errors"""
    if not isinstance(spec, dict):
        return [e("spec", "Spec must be an object", "invalid")]
    errors = []

    # Validate instances
    instances = spec.get("instances")
    if instances is not None and (not isinstance(instances, int) or instances <= 0):
        errors.append(e("spec.instances", "Instances must be a positive integer", "invalid"))

    # Validate runtime
    runtime = spec.get("runtime")
    if runtime is not None:
        if not isinstance(runtime, str):
            errors.append(e("spec.runtime", "Runtime must be a string", "invalid"))
        elif not RUNTIME_PATTERN.match(runtime):
            errors.append(e("spec.runtime", "Runtime must be in major.minor.patch format", "invalid"))
        else:
            try:
                for part in runtime.split("."):
                    if int(part) < 0:
                        errors.append(e("spec.runtime", "Runtime parts must be non-negative integers", "invalid"))
                        break
            except ValueError:
                errors.append(e("spec.runtime", "Runtime must be in major.minor.patch format", "invalid"))

    # Validate resources
    resources = spec.get("resources")
    if resources is not None:
        if not isinstance(resources, dict):
            errors.append(e("spec.resources", "Resources must be an object", "invalid"))
        else:
            for name, field in [("memory", "memory"), ("cpu", "cpu")]:
                res = resources.get(name)
                if res is not None:
                    if not isinstance(res, dict):
                        errors.append(e(f"spec.resources.{name}", f"{name.capitalize()} must be an object", "invalid"))
                    else:
                        limit = res.get("limit")
                        if limit is None:
                            errors.append(e(f"spec.resources.{name}.limit", f"{name.capitalize()} limit is required", "required"))
                        else:
                            try:
                                parse_quantity(str(limit))
                            except ValueError as ex:
                                errors.append(e(f"spec.resources.{name}.limit", f"Invalid {name} quantity: {ex}", "invalid"))

                        request = res.get("request")
                        if request is not None and limit is not None:
                            try:
                                if to_bytes(str(request)) > to_bytes(str(limit)):
                                    errors.append(e(f"spec.resources.{name}.request", "Request cannot exceed limit", "invalid"))
                            except (ValueError, AttributeError):
                                pass

    # Validate access
    access = spec.get("access")
    if access is not None:
        if not isinstance(access, dict):
            errors.append(e("spec.access", "Access must be an object", "invalid"))
        else:
            auth = access.get("authentication")
            if auth is not None and not isinstance(auth, dict):
                errors.append(e("spec.access.authentication", "Authentication must be an object", "invalid"))
            elif auth is not None:
                enabled = auth.get("enabled")
                if enabled is not None and not isinstance(enabled, bool):
                    errors.append(e("spec.access.authentication.enabled", "Enabled must be a boolean", "invalid"))

    # Validate migration
    migration = spec.get("migration")
    if migration is not None:
        if not isinstance(migration, dict):
            errors.append(e("spec.migration", "Migration must be an object", "invalid"))
        else:
            strategy = migration.get("strategy")
            if strategy is not None and strategy != "FullStop":
                errors.append(e("spec.migration.strategy", "Strategy must be 'FullStop'", "invalid"))

    # Check for forbidden autoScaling fields
    def check_forbidden(obj, path):
        if not isinstance(obj, dict):
            return
        for key, value in obj.items():
            curr = f"{path}.{key}" if path else key
            if key == "autoScaling":
                errors.append(e(curr, "autoScaling field is forbidden", "forbidden"))
            else:
                check_forbidden(value, curr)

    check_forbidden(spec, "spec")
    return errors


def validate_resource(data, check_duplicate=True):
    """Validate a complete resource document. Returns list of error dicts."""
    if not isinstance(data, dict):
        return [e("", "Document must be a mapping", "parse")]
    errors = []

    metadata = data.get("metadata")
    if metadata is None:
        errors.append(e("metadata", "Metadata is required", "required"))
    elif not isinstance(metadata, dict):
        errors.append(e("metadata", "Metadata must be an object", "required"))
    else:
        errors.extend(validate_name(metadata.get("name")))

    errors.extend(validate_spec(data.get("spec", {})))

    if check_duplicate and not errors:
        name = data.get("metadata", {}).get("name")
        if name and resource_exists(name):
            errors.append(e("metadata.name", f"Mesh '{name}' already exists", "duplicate"))

    return errors


def resource_path(name):
    """Get the file path for a resource by name"""
    return DATA_DIR / f"{name}.json"


def resource_exists(name):
    """Check if a resource with the given name exists"""
    return resource_path(name).exists()


def load_resource(name):
    """Load a resource by name"""
    path = resource_path(name)
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def save_resource(name, data):
    """Save a resource to disk"""
    DATA_DIR.mkdir(exist_ok=True)
    with open(resource_path(name), "w") as f:
        json.dump(data, f, indent=2)


def delete_resource(name):
    """Delete a resource by name. Returns True if deleted."""
    path = resource_path(name)
    if path.exists():
        path.unlink()
        return True
    return False


def list_resources():
    """List all resources as summaries"""
    resources = []
    DATA_DIR.mkdir(exist_ok=True)
    for path in DATA_DIR.glob("*.json"):
        try:
            with open(path) as f:
                resources.append(json.load(f))
        except (json.JSONDecodeError, IOError):
            continue
    return sorted(
        [{"name": r["metadata"]["name"], "status": r.get("status", {"state": "Unknown"})} for r in resources],
        key=lambda x: x["name"]
    )


def apply_defaults(data):
    """Apply all defaults to a resource specification"""
    spec = data.setdefault("spec", {})

    if "instances" not in spec:
        spec["instances"] = 1

    resources = spec.setdefault("resources", {})
    memory = resources.setdefault("memory", {"limit": "1Gi", "request": "1Gi"})
    if memory.get("request") is None and memory.get("limit") is not None:
        memory["request"] = memory["limit"]

    if "cpu" in resources and resources["cpu"] is not None:
        cpu = resources["cpu"]
        if cpu.get("request") is None and cpu.get("limit") is not None:
            cpu["request"] = cpu["limit"]

    access = spec.setdefault("access", {})
    auth = access.setdefault("authentication", {"enabled": True})
    if auth.get("enabled") is None:
        auth["enabled"] = True

    migration = spec.setdefault("migration", {})
    if "strategy" not in migration:
        migration["strategy"] = "FullStop"

    return data


def create_resource(data):
    """Create and persist a new resource"""
    data = apply_defaults(data)
    data["status"] = {"state": "Running"}
    save_resource(data["metadata"]["name"], data)
    return data


def read_yaml(path):
    """Read and parse a YAML file. Returns (data, [errors])"""
    try:
        with open(path) as f:
            content = f.read()
            if not content.strip():
                return None, [e("", "File is empty", "parse")]
            data = yaml.safe_load(content)
            if data is None:
                return None, [e("", "File is empty or contains only whitespace", "parse")]
            return data, []
    except FileNotFoundError:
        return None, [e("", f"File not found: {path}", "parse")]
    except yaml.YAMLError as ex:
        return None, [e("", f"YAML parse error: {ex}", "parse")]
    except IOError as ex:
        return None, [e("", f"Error reading file: {ex}", "parse")]


def output_json(data):
    """Output data as JSON"""
    print(json.dumps(data))


def handle_create(path):
    """Handle mesh create -f <path> command"""
    data, errs = read_yaml(path)
    if errs:
        return output_json({"errors": errs})
    errs = validate_resource(data, check_duplicate=True)
    if errs:
        return output_json({"errors": errs})
    output_json(create_resource(data))


def handle_list():
    """Handle mesh list command"""
    output_json(list_resources())


def handle_describe(name):
    """Handle mesh describe <name> command"""
    if not resource_exists(name):
        return output_json({"errors": [e("metadata.name", f"Mesh '{name}' not found", "not_found")]})
    output_json(load_resource(name))


def handle_delete(name):
    """Handle mesh delete <name> command"""
    if not resource_exists(name):
        return output_json({"errors": [e("metadata.name", f"Mesh '{name}' not found", "not_found")]})
    delete_resource(name)
    output_json({"message": f"Mesh '{name}' deleted", "metadata": {"name": name}})


def print_usage():
    print("Usage: python meshctl.py <operation> [arguments]")
    print("\nOperations:")
    print("  create    Create a mesh from a YAML file")
    print("  list      List all meshes")
    print("  describe  Describe a mesh")
    print("  delete    Delete a mesh")


def main():
    if len(sys.argv) < 2:
        print_usage()
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "create":
        if "-f" in sys.argv:
            idx = sys.argv.index("-f")
            if idx + 1 < len(sys.argv):
                handle_create(sys.argv[idx + 1])
            else:
                print("Error: -f requires a file path")
                sys.exit(1)
        else:
            print("Error: create requires -f <file>")
            sys.exit(1)
    elif cmd == "list":
        handle_list()
    elif cmd == "describe":
        if len(sys.argv) > 2:
            handle_describe(sys.argv[2])
        else:
            print("Error: describe requires a name")
            sys.exit(1)
    elif cmd == "delete":
        if len(sys.argv) > 2:
            handle_delete(sys.argv[2])
        else:
            print("Error: delete requires a name")
            sys.exit(1)
    else:
        print_usage()
        sys.exit(1)


if __name__ == "__main__":
    main()
