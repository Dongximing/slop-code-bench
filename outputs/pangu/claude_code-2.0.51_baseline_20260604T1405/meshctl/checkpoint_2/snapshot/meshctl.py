#!/usr/bin/env python3
"""meshctl: Mesh Lifecycle and Topology Control Tool"""

import argparse
import json
import os
import sys
from typing import Any, Optional

import yaml

# Persistent storage directory
STORAGE_DIR = ".mesh_storage"
DEFAULT_STORAGE_SIZE = "1Gi"


def ensure_storage() -> None:
    if not os.path.exists(STORAGE_DIR):
        os.makedirs(STORAGE_DIR)


def get_mesh_path(name: str) -> str:
    return os.path.join(STORAGE_DIR, f"{name}.json")


def load_mesh(name: str) -> Optional[dict]:
    p = get_mesh_path(name)
    if not os.path.exists(p):
        return None
    with open(p, "r") as f:
        return json.load(f)


def save_mesh(name: str, data: dict) -> None:
    ensure_storage()
    with open(get_mesh_path(name), "w") as f:
        json.dump(data, f, indent=2)


def delete_mesh(name: str) -> bool:
    p = get_mesh_path(name)
    if os.path.exists(p):
        os.remove(p)
        return True
    return False


def mesh_exists(name: str) -> bool:
    return os.path.exists(get_mesh_path(name))


def list_mesh_names() -> list:
    ensure_storage()
    names = []
    for fn in os.listdir(STORAGE_DIR):
        if fn.endswith(".json"):
            names.append(fn[:-5])
    return sorted(names)


def to_json_error(field: str, error_type: str, message: str) -> dict:
    return {"field": field, "type": error_type, "message": message}


def output_errors(errors: list, exit_code: int = 1) -> None:
    print(json.dumps({"errors": errors}, indent=2))
    sys.exit(exit_code)


def output_result(data: dict) -> None:
    print(json.dumps(data, indent=2))


def err_not_found(name: str) -> None:
    output_errors([to_json_error("metadata.name", "not_found", f"mesh '{name}' not found")])


def parse_yaml(path: str) -> tuple[Optional[dict], list]:
    errors = []
    try:
        with open(path, "r") as f:
            content = f.read()
    except IOError as e:
        errors.append(to_json_error("", "parse", f"cannot read file: {e}"))
        return None, errors
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as e:
        errors.append(to_json_error("", "parse", f"cannot parse YAML: {e}"))
        return None, errors
    return data, errors


def is_valid_memory_quantity(val: Any) -> bool:
    if not isinstance(val, str):
        return False
    import re
    m = re.match(r"^(\d+)(Ki|Mi|Gi|Ti)?$", val, re.IGNORECASE)
    if not m:
        return False
    return int(m.group(1)) >= 0


def validate_storage(storage: Any, field_path: str, errors: list) -> list:
    if storage is None:
        return errors
    if not isinstance(storage, dict):
        errors.append(to_json_error(f"{field_path}.storage", "invalid",
                                    f"{field_path}.storage must be an object"))
        return errors
    size = storage.get("size")
    if size is not None and not is_valid_memory_quantity(size):
        errors.append(to_json_error(f"{field_path}.storage.size", "invalid",
                                    f"{field_path}.storage.size must be a valid memory quantity (got '{size}')"))
    ephemeral = storage.get("ephemeral")
    if ephemeral is not None and not isinstance(ephemeral, bool):
        errors.append(to_json_error(f"{field_path}.storage.ephemeral", "invalid",
                                    "spec.network.storage.ephemeral must be a boolean"))
    cls = storage.get("className")
    if cls is not None and not isinstance(cls, str):
        errors.append(to_json_error(f"{field_path}.storage.className", "invalid",
                                    "spec.network.storage.className must be a string"))
    return errors


def validate_mesh(data: dict, is_update: bool = False, original_mesh: Optional[dict] = None) -> list:
    errors = []
    if not isinstance(data, dict):
        errors.append(to_json_error("", "parse", "YAML must be a mapping"))
        return errors
    # metadata.name required
    meta = data.get("metadata")
    if not isinstance(meta, dict):
        errors.append(to_json_error("metadata", "required", "metadata is required"))
        return errors
    name = meta.get("name")
    if not name:
        errors.append(to_json_error("metadata.name", "required", "name is required"))
    elif not isinstance(name, str):
        errors.append(to_json_error("metadata.name", "invalid", "metadata.name must be a string"))
    # spec required
    spec = data.get("spec")
    if not isinstance(spec, dict):
        errors.append(to_json_error("spec", "required", "spec is required"))
        return errors
    # instances: non-negative integer
    instances = spec.get("instances")
    if instances is not None and not isinstance(instances, int):
        errors.append(to_json_error("spec.instances", "invalid", "spec.instances must be a non-negative integer"))
    elif instances is not None and instances < 0:
        errors.append(to_json_error("spec.instances", "invalid", "spec.instances must be a non-negative integer"))
    # network
    network = spec.get("network")
    if network is not None:
        if not isinstance(network, dict):
            errors.append(to_json_error("spec.network", "invalid", "spec.network must be an object"))
        else:
            storage = network.get("storage")
            errors = validate_storage(storage, "spec.network", errors)
            rf = network.get("replicationFactor")
            if rf is not None:
                if not isinstance(rf, int) or rf < 1:
                    errors.append(to_json_error("spec.network.replicationFactor", "invalid",
                                                 f"spec.network.replicationFactor must be a positive integer (got '{rf}')"))
                elif instances is not None and rf > instances:
                    errors.append(to_json_error("spec.network.replicationFactor", "invalid",
                                                 f"spec.network.replicationFactor ({rf}) must not exceed spec.instances ({instances})"))
    # Immutability check on update
    if is_update and original_mesh:
        orig_storage = original_mesh.get("spec", {}).get("network", {}).get("storage")
        new_storage = spec.get("network", {}).get("storage")
        if orig_storage and new_storage:
            orig_size = orig_storage.get("size")
            new_size = new_storage.get("size")
            if orig_size is not None and new_size is not None and orig_size != new_size:
                errors.append(to_json_error("spec.network.storage.size", "immutable",
                                            "field 'spec.network.storage.size' is immutable after creation"))
    return errors


def storage_output(storage: dict) -> dict:
    if storage.get("ephemeral"):
        return {"ephemeral": True}
    return {"ephemeral": storage.get("ephemeral", False), "size": storage["size"]}


def default_replication_factor(instances: int) -> int:
    return 1 if instances > 0 else 1  # computed default based on instances


def create_conditions() -> list:
    return [
        {"type": "Healthy", "status": "True", "message": ""},
        {"type": "PrechecksPassed", "status": "True", "message": ""}
    ]


def create_status(spec: dict) -> dict:
    instances = spec.get("instances")
    if instances is None:
        instances = 0
    if instances > 0:
        state = "Running"
        ready = instances
        starting = 0
        stopped = 0
    else:
        state = "Stopped"
        ready = 0
        starting = 0
        stopped = 0
    return {
        "state": state,
        "stable": True,
        "conditions": create_conditions(),
        "instances": {"ready": ready, "starting": starting, "stopped": stopped}
    }


def apply_defaults(data: dict) -> dict:
    """Apply defaults not already set."""
    spec = data.get("spec", {})
    network = spec.get("network", {})
    storage = network.get("storage")
    if isinstance(storage, dict) and "size" not in storage:
        storage["size"] = DEFAULT_STORAGE_SIZE
    if "instances" not in spec:
        spec["instances"] = 0
    if "replicationFactor" not in network or network.get("replicationFactor") is None:
        network["replicationFactor"] = default_replication_factor(spec["instances"])
    if "network" in spec:
        spec["network"] = network
    data["spec"] = spec
    return data


def sort_conditions(conditions: list) -> None:
    conditions.sort(key=lambda c: c["type"])


def has_condition(conditions: list, cond_type: str) -> bool:
    return any(c["type"] == cond_type for c in conditions)


def filter_condition(conditions: list, cond_type: str) -> list:
    """Remove condition of given type."""
    return [c for c in conditions if c["type"] != cond_type]


def cmd_create(args) -> None:
    data, errors = parse_yaml(args.file)
    if errors:
        output_errors(errors)
    name = data.get("metadata", {}).get("name")
    if not name:
        output_errors([to_json_error("metadata.name", "required", "name is required")])
    if mesh_exists(name):
        output_errors([to_json_error("metadata.name", "already_exists", f"mesh '{name}' already exists")])
    errors = validate_mesh(data)
    if errors:
        output_errors(errors)
    data = apply_defaults(data)
    data["status"] = create_status(data["spec"])
    save_mesh(name, data)
    output_result(data)


def cmd_list(args) -> None:
    names = list_mesh_names()
    result = []
    for n in names:
        m = load_mesh(n)
        st = m.get("status", {})
        result.append({
            "name": n,
            "instances": m["spec"].get("instances", 0),
            "state": st.get("state", "Unknown"),
            "stable": st.get("stable", True)
        })
    output_result(result)


def cmd_describe(args) -> None:
    name = args.name
    if not mesh_exists(name):
        err_not_found(name)
    mesh = load_mesh(name)
    # Apply post-describe state transitions
    spec = mesh["spec"]
    status = mesh.get("status", {})
    inst = status.get("instances", {})
    # Remove transient Scaling
    conditions = [c for c in status.get("conditions", []) if c["type"] != "Scaling"]
    # Update ready to match spec.instances and starting to 0
    new_instances = {
        "ready": spec.get("instances", 0),
        "starting": 0,
        "stopped": inst.get("stopped", 0)
    }
    status["instances"] = new_instances
    status["conditions"] = conditions
    status["stable"] = True
    status["state"] = "Running" if spec.get("instances", 0) > 0 else "Stopped"
    mesh["status"] = status
    sort_conditions(status["conditions"])
    save_mesh(name, mesh)
    output_result(mesh)


def cmd_delete(args) -> None:
    name = args.name
    if not mesh_exists(name):
        err_not_found(name)
    delete_mesh(name)
    output_result({"message": f"Mesh '{name}' deleted successfully", "metadata": {"name": name}})


def cmd_update(args) -> None:
    data, errors = parse_yaml(args.file)
    if errors:
        output_errors(errors)
    name = data.get("metadata", {}).get("name")
    if not name:
        output_errors([to_json_error("metadata.name", "required", "metadata.name is required")])
    if not mesh_exists(name):
        err_not_found(name)
    original = load_mesh(name)
    original_spec = original["spec"]  # Keep original before merge
    # Validate update
    errors = validate_mesh(data, is_update=True, original_mesh=original)
    if errors:
        output_errors(errors)
    # Merge update spec into original
    new_spec = merge_spec(original["spec"], data["spec"])
    original["spec"] = new_spec
    original["_original_spec"] = original_spec  # Store for lifecycle computation
    # Compute new status based on lifecycle transitions
    status = apply_lifecycle_transitions(original, new_spec)
    original["status"] = status
    # Format storage output in spec
    net = original["spec"].get("network", {})
    if "storage" in net:
        net["storage"] = storage_output(net["storage"])
    original["spec"]["network"] = net
    # Cleanup
    if "_original_spec" in original:
        del original["_original_spec"]
    save_mesh(name, original)
    sort_conditions(status["conditions"])
    output_result(original)


def merge_spec(orig_spec: dict, update_spec: dict) -> dict:
    merged = {k: v for k, v in orig_spec.items()}
    # Deep merge instances
    if "instances" in update_spec:
        merged["instances"] = update_spec["instances"]
    # Deep merge network
    orig_net = orig_spec.get("network", {})
    update_net = update_spec.get("network", {})
    merged_net = {k: v for k, v in orig_net.items()}
    if "storage" in update_net:
        # Merge storage field-by-field
        orig_storage = orig_net.get("storage", {})
        new_storage = update_net["storage"]
        merged_storage = {"size": orig_storage.get("size"),
                          "ephemeral": orig_storage.get("ephemeral", False),
                          "className": orig_storage.get("className")}
        if new_storage.get("size") is not None:
            merged_storage["size"] = new_storage["size"]
        if new_storage.get("ephemeral") is not None:
            merged_storage["ephemeral"] = new_storage["ephemeral"]
        if new_storage.get("className") is not None:
            merged_storage["className"] = new_storage["className"]
        merged_net["storage"] = merged_storage
    if "replicationFactor" in update_net:
        merged_net["replicationFactor"] = update_net["replicationFactor"]
    merged["network"] = merged_net
    return merged


def apply_lifecycle_transitions(mesh: dict, new_spec: dict) -> dict:
    """Compute new status based on lifecycle transitions."""
    # Use _original_spec to get the spec BEFORE merge, if available
    orig_spec = mesh.get("_original_spec", mesh.get("spec", {}))
    orig_instances = orig_spec.get("instances", 0)
    new_instances = new_spec.get("instances", 0)
    orig_status = mesh.get("status", {})
    orig_conditions = list(orig_status.get("conditions", []))
    orig_inst_status = orig_status.get("instances", {})
    orig_ready = orig_inst_status.get("ready", orig_instances)
    orig_stopped = orig_inst_status.get("stopped", 0)

    # Handle resume from stopped first
    desired_resume = orig_status.get("desiredInstancesOnResume")
    is_stopped = orig_status.get("state") == "Stopped"
    was_stopped = desired_resume is not None and is_stopped

    if was_stopped and new_instances > 0:
        # Resume
        target = new_instances if new_instances > 0 else desired_resume
        # Remove GracefulShutdown and desiredInstancesOnResume
        orig_conditions = filter_condition(orig_conditions, "GracefulShutdown")
        new_status = {
            "state": "Running",
            "stable": False,
            "conditions": orig_conditions,
            "instances": {"ready": 0, "starting": target, "stopped": 0}
        }
        # The status itself should not have desiredInstancesOnResume after resume
        return new_status

    # Stop
    if orig_instances > 0 and new_instances == 0:
        orig_conditions.append({"type": "GracefulShutdown", "status": "True", "message": ""})
        total_stopped = orig_ready + orig_stopped
        return {
            "state": "Stopped",
            "stable": False,
            "conditions": orig_conditions,
            "instances": {"ready": 0, "starting": 0, "stopped": total_stopped},
            "desiredInstancesOnResume": orig_instances
        }

    # Scale up
    if new_instances > orig_instances:
        diff = new_instances - orig_instances
        orig_conditions.append({"type": "Scaling", "status": "True",
                                 "message": f"Scaling from {orig_instances} to {new_instances} instances"})
        return {
            "state": "Running",
            "stable": False,
            "conditions": orig_conditions,
            "instances": {"ready": orig_instances, "starting": diff, "stopped": orig_stopped}
        }

    # Scale down
    if new_instances < orig_instances:
        orig_conditions.append({"type": "Scaling", "status": "True", "message": ""})
        return {
            "state": "Running",
            "stable": False,
            "conditions": orig_conditions,
            "instances": {"ready": new_instances, "starting": 0, "stopped": orig_stopped}
        }

    # No change
    # Remove any transient Scaling from previous scale operations
    orig_conditions = filter_condition(orig_conditions, "Scaling")
    return {
        "state": "Running" if new_instances > 0 else "Stopped",
        "stable": True,
        "conditions": orig_conditions,
        "instances": {"ready": new_instances, "starting": 0, "stopped": orig_stopped}
    }


def main() -> None:
    parser = argparse.ArgumentParser(prog="meshctl", description="Mesh Lifecycle and Topology Control Tool")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # mesh subcommand
    mesh_parser = subparsers.add_parser("mesh")
    mesh_subparsers = mesh_parser.add_subparsers(dest="operation", required=True)

    # mesh create
    c = mesh_subparsers.add_parser("create")
    c.add_argument("-f", "--file", required=True, help="YAML file path")
    c.set_defaults(func=cmd_create)

    # mesh list
    l = mesh_subparsers.add_parser("list")
    l.set_defaults(func=cmd_list)

    # mesh describe
    d = mesh_subparsers.add_parser("describe")
    d.add_argument("name", help="Mesh name")
    d.set_defaults(func=cmd_describe)

    # mesh delete
    de = mesh_subparsers.add_parser("delete")
    de.add_argument("name", help="Mesh name")
    de.set_defaults(func=cmd_delete)

    # mesh update
    u = mesh_subparsers.add_parser("update")
    u.add_argument("-f", "--file", required=True, help="YAML file path")
    u.set_defaults(func=cmd_update)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
