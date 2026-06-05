#!/usr/bin/env python3
"""Mesh Lifecycle and Topology CLI Tool"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


# Data storage for meshes
_MESHES: Dict[str, Dict] = {}


def load_meshes_storage() -> Dict[str, Dict]:
    """Load meshes from persistent storage."""
    storage_file = "/tmp/meshes.json"
    if os.path.exists(storage_file):
        with open(storage_file, "r") as f:
            return json.load(f)
    return {}


def save_meshes_storage(data: Dict):
    """Save meshes to persistent storage."""
    storage_file = "/tmp/meshes.json"
    with open(storage_file, "w") as f:
        json.dump(data, f, indent=2)


def load_vaults_storage() -> Dict[str, Dict]:
    """Load vaults from persistent storage."""
    storage_file = "/tmp/vaults.json"
    if os.path.exists(storage_file):
        with open(storage_file, "r") as f:
            return json.load(f)
    return {}


def save_vaults_storage(data: Dict):
    """Save vaults to persistent storage."""
    storage_file = "/tmp/vaults.json"
    with open(storage_file, "w") as f:
        json.dump(data, f, indent=2)


def create_error(field: str, message: str, error_type: str) -> Dict[str, str]:
    """Create an error object."""
    return {
        "field": field,
        "type": error_type,
        "message": message
    }


def parse_memory_quantity(value: str) -> bool:
    """Parse memory quantity format (non-negative integer with optional Ki, Mi, Gi, Ti)."""
    if not isinstance(value, str):
        return False
    pattern = r'^\d+(Ki|Mi|Gi|Ti)?$'
    return bool(re.match(pattern, value, re.IGNORECASE))


def validate_instances(instances: Any) -> List[Dict[str, str]]:
    """Validate instances field."""
    errors = []
    if instances is None:
        return errors
    if not isinstance(instances, int):
        errors.append(create_error("spec.instances", f"instances must be an integer", "invalid"))
    elif instances < 0:
        errors.append(create_error("spec.instances", f"instances must be non-negative", "invalid"))
    return errors


def validate_storage_size(size: Any) -> List[Dict[str, str]]:
    """Validate storage size field."""
    errors = []
    if not isinstance(size, str):
        errors.append(create_error("spec.network.storage.size", "size must be a string", "invalid"))
        return errors
    if not parse_memory_quantity(size):
        errors.append(create_error("spec.network.storage.size", f"'{size}' is not a valid memory quantity", "invalid"))
    return errors


def validate_replication_factor(rf: Any, instances: int) -> List[Dict[str, str]]:
    """Validate replication factor against instances."""
    errors = []
    if rf is None:
        return errors
    if not isinstance(rf, int):
        errors.append(create_error("spec.network.replicationFactor", "replicationFactor must be an integer", "invalid"))
        return errors
    if rf < 1:
        errors.append(create_error("spec.network.replicationFactor", f"replicationFactor must be at least 1, got {rf}", "invalid"))
    elif instances > 0 and rf > instances:
        errors.append(create_error("spec.network.replicationFactor", f"replicationFactor ({rf}) cannot exceed instances ({instances})", "invalid"))
    return errors


def compute_default_replication_factor(instances: int) -> int:
    """Compute default replication factor based on instance count."""
    if instances <= 0:
        return 1
    return min(3, instances)


def validate_mesh(data: Dict, update: bool = False, existing: Optional[Dict] = None) -> List[Dict[str, str]]:
    """Validate mesh data."""
    errors = []

    # Validate metadata.name
    metadata = data.get("metadata", {})
    name = metadata.get("name")
    if not name or not isinstance(name, str):
        if update:
            errors.append(create_error("metadata.name", "metadata.name is required", "required"))
        else:
            errors.append(create_error("metadata.name", "metadata.name is required", "required"))

    if not update and name:
        # Check for duplicates on create
        storage = load_meshes_storage()
        if name in storage:
            errors.append(create_error("metadata.name", f"mesh '{name}' already exists", "duplicate"))

    spec = data.get("spec", {})

    # Validate spec.instances
    instances = spec.get("instances")
    errors.extend(validate_instances(instances))

    # Validate storage.size
    network = spec.get("network", {})
    storage = network.get("storage", {})
    size = storage.get("size")
    if size is not None:
        errors.extend(validate_storage_size(size))

    # Validate replication factor
    instances_val = instances if isinstance(instances, int) else (existing.get("spec", {}).get("instances", 0) if existing else 0)
    rf = network.get("replicationFactor")
    errors.extend(validate_replication_factor(rf, instances_val))

    # Check for immutable field changes on update
    if update and existing:
        existing_storage = existing.get("spec", {}).get("network", {}).get("storage", {})
        existing_size = existing_storage.get("size")
        new_size = storage.get("size")
        if existing_size is not None and new_size is not None and existing_size != new_size:
            errors.append(create_error("spec.network.storage.size", "size is immutable after creation", "immutable"))

        # Also check if updating from 0 to instances when it was stopped with desiredInstancesOnResume
        if "desiredInstancesOnResume" in existing.get("status", {}):
            # This is a resume operation, check spec.instances
            pass  # Resume is allowed

    return errors


def validate_update_policy(policy: Any) -> List[Dict[str, str]]:
    """Validate updatePolicy field for vault."""
    errors = []
    if policy is None:
        return errors
    if policy not in ("retain", "recreate"):
        errors.append(create_error("spec.updatePolicy", f"'{policy}' is not a valid updatePolicy, must be 'retain' or 'recreate'", "invalid"))
    return errors


def validate_vault(data: Dict, update: bool = False, existing: Optional[Dict] = None) -> List[Dict[str, str]]:
    """Validate vault data."""
    errors = []

    # Validate metadata.name
    metadata = data.get("metadata", {})
    name = metadata.get("name")
    if not name or not isinstance(name, str):
        errors.append(create_error("metadata.name", "metadata.name is required", "required"))
        return errors  # Can't continue without name

    if not update and name:
        # Check for duplicates on create - vaults only
        vaults_storage = load_vaults_storage()
        # Check for duplicate metadata name across all vaults
        for vault_name, vault in vaults_storage.items():
            if vault.get("metadata", {}).get("name") == name:
                errors.append(create_error("metadata.name", f"vault '{name}' already exists", "duplicate"))
                return errors

    spec = data.get("spec", {})

    # Validate spec.meshRef (required)
    mesh_ref = spec.get("meshRef")
    if not mesh_ref or not isinstance(mesh_ref, str):
        errors.append(create_error("spec.meshRef", "spec.meshRef is required", "required"))
        return errors

    # On create and update, check if mesh exists
    meshes_storage = load_meshes_storage()
    if mesh_ref not in meshes_storage:
        errors.append(create_error("spec.meshRef", f'mesh "{mesh_ref}" not found', "invalid"))
        return errors  # Can't continue without valid meshRef

    # Validate spec.updatePolicy
    update_policy = spec.get("updatePolicy")
    errors.extend(validate_update_policy(update_policy))

    # Validate template exclusivity
    has_template = spec.get("template") is not None
    has_template_ref = spec.get("templateRef") is not None
    if has_template and has_template_ref:
        errors.append(create_error("spec.template", "only one of spec.template and spec.templateRef may be specified", "invalid"))

    if not update and name:
        # For create: check for duplicate (meshRef, vaultName) pair
        vaults_storage = load_vaults_storage()
        vault_name = spec.get("vaultName", name)
        for existing_vault in vaults_storage.values():
            existing_spec = existing_vault.get("spec", {})
            existing_mesh_ref = existing_spec.get("meshRef")
            existing_vault_name = existing_spec.get("vaultName", existing_vault.get("metadata", {}).get("name"))
            if existing_mesh_ref == mesh_ref and existing_vault_name == vault_name:
                errors.append(create_error("spec.vaultName", f"vault with identity meshRef='{mesh_ref}' and vaultName='{vault_name}' already exists", "duplicate"))
                break

    if update and existing:
        # Check for immutable meshRef change
        existing_mesh_ref = existing.get("spec", {}).get("meshRef")
        if existing_mesh_ref is not None and mesh_ref != existing_mesh_ref:
            errors.append(create_error("spec.meshRef", f"spec.meshRef is immutable, current value is '{existing_mesh_ref}'", "immutable"))

        # Check for immutable vaultName change
        existing_vault_name = existing.get("spec", {}).get("vaultName")
        new_vault_name = spec.get("vaultName")
        if existing_vault_name is not None and new_vault_name is not None and existing_vault_name != new_vault_name:
            errors.append(create_error("spec.vaultName", f"spec.vaultName is immutable, current value is '{existing_vault_name}'", "immutable"))

    return errors


def validate_mesh_delete_for_vaults(mesh_name: str) -> List[Dict[str, str]]:
    """Check if any vaults reference the given mesh. Returns errors if vaults exist."""
    errors = []
    vaults_storage = load_vaults_storage()
    dependent_vaults = []
    for vault_name, vault in vaults_storage.items():
        mesh_ref = vault.get("spec", {}).get("meshRef")
        if mesh_ref == mesh_name:
            dependent_vaults.append(vault.get("metadata", {}).get("name", vault_name))

    if dependent_vaults:
        errors.append(create_error("metadata.name", f"mesh '{mesh_name}' cannot be deleted because it is referenced by vaults: {', '.join(dependent_vaults)}", "conflict"))

    return errors


def format_storage_for_output(storage: Dict) -> Dict:
    """Format storage for output according to ephemeral rules."""
    if storage.get("ephemeral") is True:
        return {"ephemeral": True}
    result = {"ephemeral": storage.get("ephemeral", False)}
    if storage.get("size") is not None:
        result["size"] = storage["size"]
    return result


def prepare_mesh_for_output(mesh: Dict) -> Dict:
    """Prepare a mesh for output, applying ephemeral storage formatting."""
    mesh = mesh.copy()
    spec = mesh.get("spec", {})
    if "network" in spec:
        network = spec["network"].copy()
        if "storage" in network:
            network["storage"] = format_storage_for_output(network["storage"])
        spec["network"] = network

    return mesh


def merge_storage(existing_storage: Dict, new_storage: Dict) -> Dict:
    """Merge storage configuration according to merge rules."""
    result = existing_storage.copy() if existing_storage else {}

    # If ephemeral is explicitly provided, use it (not immutable)
    if "ephemeral" in new_storage:
        result["ephemeral"] = new_storage["ephemeral"]
    elif "ephemeral" not in result:
        result["ephemeral"] = False

    # className is not immutable
    if "className" in new_storage:
        result["className"] = new_storage["className"]
    elif "className" not in result:
        result["className"] = None

    # size is immutable - only use new size if existing doesn't have it
    if "size" in new_storage:
        if "size" not in result:
            result["size"] = new_storage["size"]
        # If existing already has size, don't change it (immutable)
    elif "size" not in result:
        result["size"] = "1Gi"  # Default

    return result


def merge_network(existing_network: Dict, new_network: Dict) -> Dict:
    """Merge network configuration according to merge rules."""
    result = existing_network.copy() if existing_network else {}

    # Merge storage
    if "storage" in new_network:
        existing_storage = result.get("storage", {})
        result["storage"] = merge_storage(existing_storage, new_network["storage"])

    # replicationFactor is a leaf field - replaces
    if "replicationFactor" in new_network:
        result["replicationFactor"] = new_network["replicationFactor"]
    elif "replicationFactor" not in result:
        # Only set default if not present in existing
        # Don't compute default if omitted during update
        pass

    return result


def merge_spec(existing_spec: Dict, new_spec: Dict, existing: Dict) -> Dict:
    """Merge spec configuration according to merge rules."""
    result = existing_spec.copy() if existing_spec else {}

    # instances is a leaf field - replaces (if present)
    if "instances" in new_spec:
        result["instances"] = new_spec["instances"]
    elif "instances" not in result:
        result["instances"] = 1  # Default

    # Merge network
    if "network" in new_spec:
        existing_network = result.get("network", {})
        result["network"] = merge_network(existing_network, new_spec["network"])

    return result


def merge_mesh(existing: Dict, new: Dict) -> Dict:
    """Merge new mesh configuration into existing mesh."""
    result = existing.copy()

    # Merge spec
    if "spec" in new:
        existing_spec = result.get("spec", {})
        result["spec"] = merge_spec(existing_spec, new["spec"], existing)

    return result


def handle_instance_lifecycle(mesh: Dict, is_update_response: bool = True) -> Dict:
    """Handle instance lifecycle transitions."""
    mesh = mesh.copy()
    spec = mesh.get("spec", {})
    status = mesh.get("status", {})

    current_instances = spec.get("instances", 0)
    prev_ready_instances = status.get("instances", {}).get("ready", 0)

    conditions = status.get("conditions", [])

    # Remove transient conditions from previous operations
    conditions = [c for c in conditions if c.get("type") not in ("Scaling", "GracefulShutdown")]

    # Determine operation type
    was_stopped = status.get("state") == "Stopped" and "desiredInstancesOnResume" in status

    if was_stopped:
        # Resume operation
        conditions.append({
            "type": "Scaling",
            "status": "True",
            "message": "Resuming from stopped state"
        })

        # On resume: remove desiredInstancesOnResume
        mesh["status"].pop("desiredInstancesOnResume", None)
        mesh["status"]["instances"] = {
            "ready": 0,
            "starting": current_instances,
            "stopped": 0
        }
        mesh["status"]["state"] = "Running"
        mesh["status"]["stable"] = False

    elif current_instances == 0:
        # Stop operation
        if prev_ready_instances > 0:
            conditions.append({
                "type": "GracefulShutdown",
                "status": "True",
                "message": ""
            })

            mesh["status"]["desiredInstancesOnResume"] = prev_ready_instances
            mesh["status"]["instances"] = {
                "ready": 0,
                "starting": 0,
                "stopped": prev_ready_instances
            }
            mesh["status"]["state"] = "Stopped"
            mesh["status"]["stable"] = False
        else:
            mesh["status"]["state"] = "Stopped"
            mesh["status"]["instances"] = {
                "ready": 0,
                "starting": 0,
                "stopped": 0
            }
            mesh["status"]["stable"] = True

    elif current_instances > prev_ready_instances:
        # Scale up
        conditions.append({
            "type": "Scaling",
            "status": "True",
            "message": f"Scaling from {prev_ready_instances} to {current_instances} instances"
        })

        mesh["status"]["instances"] = {
            "ready": prev_ready_instances,
            "starting": current_instances - prev_ready_instances,
            "stopped": status.get("instances", {}).get("stopped", 0)
        }
        mesh["status"]["state"] = "Running"
        mesh["status"]["stable"] = False

    elif current_instances < prev_ready_instances:
        # Scale down
        conditions.append({
            "type": "Scaling",
            "status": "True",
            "message": f"Scaling down from {prev_ready_instances} to {current_instances} instances"
        })

        mesh["status"]["instances"] = {
            "ready": current_instances,
            "starting": 0,
            "stopped": status.get("instances", {}).get("stopped", 0)
        }
        mesh["status"]["state"] = "Running"
        mesh["status"]["stable"] = False

    else:
        # Steady state
        mesh["status"]["instances"] = {
            "ready": current_instances,
            "starting": 0,
            "stopped": status.get("instances", {}).get("stopped", 0)
        }
        mesh["status"]["state"] = "Running" if current_instances > 0 else "Stopped"
        mesh["status"]["stable"] = True

    mesh["status"]["conditions"] = conditions

    return mesh


def create_mesh(data: Dict) -> Dict:
    """Create a new mesh."""
    errors = validate_mesh(data, update=False)
    if errors:
        return {"errors": errors}

    name = data["metadata"]["name"]
    spec = data.get("spec", {})

    # Set default replication factor if not specified
    network = spec.get("network", {})
    if network.get("replicationFactor") is None:
        instances = spec.get("instances", 1)
        if "network" not in spec:
            spec["network"] = {}
        spec["network"]["replicationFactor"] = compute_default_replication_factor(instances)

    # Set default storage size if not specified
    storage = network.get("storage", {})
    if storage.get("size") is None:
        if "storage" not in network:
            network["storage"] = {}
        network["storage"]["size"] = "1Gi"

    # Create mesh with initial status
    instances = spec.get("instances", 1)

    mesh = {
        "apiVersion": "mesh.example.com/v1",
        "kind": "Mesh",
        "metadata": data.get("metadata", {}),
        "spec": spec,
        "status": {
            "state": "Running" if instances > 0 else "Stopped",
            "stable": True,
            "instances": {
                "ready": instances,
                "starting": 0,
                "stopped": 0
            },
            "conditions": [
                {
                    "type": "Healthy",
                    "status": "True",
                    "message": ""
                },
                {
                    "type": "PrechecksPassed",
                    "status": "True",
                    "message": ""
                }
            ]
        }
    }

    if instances == 0:
        mesh["status"]["desiredInstancesOnResume"] = 0

    # Persist
    storage = load_meshes_storage()
    storage[name] = mesh
    save_meshes_storage(storage)

    return mesh


def update_mesh(data: Dict) -> Dict:
    """Update an existing mesh."""
    name = data.get("metadata", {}).get("name")

    if not name:
        return {"errors": [create_error("metadata.name", "metadata.name is required for update", "required")]}

    storage = load_meshes_storage()
    if name not in storage:
        return {"errors": [create_error("metadata.name", f'mesh "{name}" not found', "not_found")]}

    existing = storage[name]

    # Validate
    errors = validate_mesh(data, update=True, existing=existing)
    if errors:
        return {"errors": errors}

    # Merge
    updated = merge_mesh(existing, data)

    # Handle instance lifecycle
    updated = handle_instance_lifecycle(updated)

    # Persist
    storage[name] = updated
    save_meshes_storage(storage)

    return updated


def list_meshes() -> List[Dict]:
    """List all mesh summaries."""
    storage = load_meshes_storage()
    result = []

    for name, mesh in storage.items():
        summary = {
            "metadata": {
                "name": name
            },
            "spec": {
                "instances": mesh.get("spec", {}).get("instances", 0)
            },
            "status": {
                "state": mesh.get("status", {}).get("state", "Unknown"),
                "stable": mesh.get("status", {}).get("stable", True),
                "instances": mesh.get("status", {}).get("instances", {})
            }
        }
        result.append(summary)

    return result


def describe_mesh(name: str) -> Dict:
    """Describe a specific mesh."""
    storage = load_meshes_storage()

    if name not in storage:
        return {"errors": [create_error("metadata.name", f'mesh "{name}" not found', "not_found")]}

    mesh = storage[name].copy()
    spec = mesh.get("spec", {})
    status = mesh.get("status", {})

    # On describe, omit transient conditions (Scaling, GracefulShutdown)
    conditions = status.get("conditions", [])
    conditions = [c for c in conditions if c.get("type") not in ("Scaling", "GracefulShutdown")]
    mesh["status"]["conditions"] = conditions

    # Handle lifecycle transition on describe
    instances = spec.get("instances", 0)

    # If we have an active scaling operation (stable=false and starting>0)
    if not status.get("stable", True) and status.get("instances", {}).get("starting", 0) > 0:
        # Transition to steady state: all instances are ready
        mesh["status"]["stable"] = True
        # Remove desiredInstancesOnResume if present (resume completed)
        mesh["status"].pop("desiredInstancesOnResume", None)
        mesh["status"]["instances"] = {
            "ready": instances,
            "starting": 0,
            "stopped": status.get("instances", {}).get("stopped", 0)
        }
        mesh["status"]["state"] = "Running"

    # Handle scale down transition (stable=false, starting=0)
    elif not status.get("stable", True) and status.get("instances", {}).get("starting", 0) == 0:
        # Scale down is complete - ready now equals instances
        mesh["status"]["stable"] = True
        mesh["status"]["instances"] = {
            "ready": instances,
            "starting": 0,
            "stopped": status.get("instances", {}).get("stopped", 0)
        }
        mesh["status"]["state"] = "Running" if instances > 0 else "Stopped"

    return mesh


def delete_mesh(name: str) -> Dict:
    """Delete a mesh."""
    # Check for vault dependencies first
    errors = validate_mesh_delete_for_vaults(name)
    if errors:
        return {"errors": errors}

    storage = load_meshes_storage()

    if name not in storage:
        return {"errors": [create_error("metadata.name", f'mesh "{name}" not found', "not_found")]}

    del storage[name]
    save_meshes_storage(storage)

    return {
        "message": f"Mesh '{name}' has been deleted",
        "metadata": {"name": name}
    }


# Vault Functions

def create_vault(data: Dict) -> Dict:
    """Create a new vault."""
    errors = validate_vault(data, update=False)
    if errors:
        return {"errors": errors}

    name = data["metadata"]["name"]
    spec = data.get("spec", {})

    # Set default vaultName to metadata.name if not specified
    if spec.get("vaultName") is None:
        spec = spec.copy()
        spec["vaultName"] = name

    # Set default updatePolicy
    if spec.get("updatePolicy") is None:
        spec = spec.copy()
        spec["updatePolicy"] = "retain"

    # Determine parent mesh stability
    meshes_storage = load_meshes_storage()
    mesh_name = spec["meshRef"]
    mesh = meshes_storage.get(mesh_name, {})
    mesh_stable = mesh.get("status", {}).get("stable", True)

    vault = {
        "apiVersion": "mesh.example.com/v1",
        "kind": "Vault",
        "metadata": data.get("metadata", {}),
        "spec": spec,
        "status": {
            "state": "Ready" if mesh_stable else "Pending",
            "conditions": [
                {
                    "type": "Ready",
                    "status": "True" if mesh_stable else "False",
                    "message": ""
                }
            ]
        }
    }

    # Persist
    vaults_storage = load_vaults_storage()
    vaults_storage[name] = vault
    save_vaults_storage(vaults_storage)

    return vault


def list_vaults() -> List[Dict]:
    """List all vault summaries sorted by name."""
    vaults_storage = load_vaults_storage()
    result = []

    for name in sorted(vaults_storage.keys()):
        vault = vaults_storage[name]
        summary = {
            "metadata": {
                "name": vault.get("metadata", {}).get("name", name)
            },
            "spec": {
                "meshRef": vault.get("spec", {}).get("meshRef", ""),
                "vaultName": vault.get("spec", {}).get("vaultName", ""),
                "updatePolicy": vault.get("spec", {}).get("updatePolicy", "retain")
            },
            "status": {
                "state": vault.get("status", {}).get("state", "Unknown")
            }
        }
        result.append(summary)

    return result


def describe_vault(name: str) -> Dict:
    """Describe a specific vault."""
    vaults_storage = load_vaults_storage()

    if name not in vaults_storage:
        return {"errors": [create_error("metadata.name", f'vault "{name}" not found', "not_found")]}

    return vaults_storage[name]


def update_vault(data: Dict) -> Dict:
    """Update an existing vault."""
    vault_name = data.get("metadata", {}).get("name")

    if not vault_name:
        return {"errors": [create_error("metadata.name", "metadata.name is required for update", "required")]}

    vaults_storage = load_vaults_storage()
    if vault_name not in vaults_storage:
        return {"errors": [create_error("metadata.name", f'vault "{vault_name}" not found', "not_found")]}

    existing = vaults_storage[vault_name]

    # Validate
    errors = validate_vault(data, update=True, existing=existing)
    if errors:
        return {"errors": errors}

    # Merge spec (replace non-immutable fields)
    new_spec = data.get("spec", {})
    updated_spec = existing["spec"].copy()

    # Update mutable fields
    if new_spec.get("updatePolicy") is not None:
        updated_spec["updatePolicy"] = new_spec["updatePolicy"]
    if new_spec.get("template") is not None:
        updated_spec["template"] = new_spec["template"]
        # Clear templateRef when setting template
        updated_spec.pop("templateRef", None)
    if new_spec.get("templateRef") is not None:
        updated_spec["templateRef"] = new_spec["templateRef"]
        # Clear template when setting templateRef
        updated_spec.pop("template", None)

    # Update vault
    updated = {
        "apiVersion": existing["apiVersion"],
        "kind": "Vault",
        "metadata": existing["metadata"].copy(),
        "spec": updated_spec,
        "status": existing["status"].copy()
    }

    # Persist
    vaults_storage[vault_name] = updated
    save_vaults_storage(vaults_storage)

    return updated


def delete_vault(name: str) -> Dict:
    """Delete a vault."""
    vaults_storage = load_vaults_storage()

    if name not in vaults_storage:
        return {"errors": [create_error("metadata.name", f'vault "{name}" not found', "not_found")]}

    del vaults_storage[name]
    save_vaults_storage(vaults_storage)

    return {
        "message": f"Vault '{name}' has been deleted",
        "metadata": {"name": name}
    }


def output_json(obj):
    """Output object as JSON to stdout."""
    print(json.dumps(obj, indent=2))


def output_errors(errors):
    """Output errors as JSON to stdout."""
    print(json.dumps({"errors": errors}, indent=2))


def cmd_mesh_create(filepath: str):
    """Handle mesh create command."""
    try:
        with open(filepath, "r") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        output_errors([create_error("", f"File not found: {filepath}", "parse")])
        return
    except yaml.YAMLError as e:
        output_errors([create_error("", f"Failed to parse YAML: {e}", "parse")])
        return

    if data is None:
        output_errors([create_error("", "Empty YAML file", "parse")])
        return

    result = create_mesh(data)
    output_json(result)


def cmd_mesh_list():
    """Handle mesh list command."""
    result = list_meshes()
    output_json(result)


def cmd_mesh_describe(name: str):
    """Handle mesh describe command."""
    result = describe_mesh(name)
    output_json(result)


def cmd_mesh_delete(name: str):
    """Handle mesh delete command."""
    result = delete_mesh(name)
    output_json(result)


def cmd_mesh_update(filepath: str):
    """Handle mesh update command."""
    try:
        with open(filepath, "r") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        output_errors([create_error("", f"File not found: {filepath}", "parse")])
        return
    except yaml.YAMLError as e:
        output_errors([create_error("", f"Failed to parse YAML: {e}", "parse")])
        return

    if data is None:
        output_errors([create_error("", "Empty YAML file", "parse")])
        return

    result = update_mesh(data)
    output_json(result)


# Vault Command Handlers

def cmd_vault_create(filepath: str):
    """Handle vault create command."""
    try:
        with open(filepath, "r") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        output_errors([create_error("", f"File not found: {filepath}", "parse")])
        return
    except yaml.YAMLError as e:
        output_errors([create_error("", f"Failed to parse YAML: {e}", "parse")])
        return

    if data is None:
        output_errors([create_error("", "Empty YAML file", "parse")])
        return

    result = create_vault(data)
    output_json(result)


def cmd_vault_list():
    """Handle vault list command."""
    result = list_vaults()
    output_json(result)


def cmd_vault_describe(name: str):
    """Handle vault describe command."""
    result = describe_vault(name)
    output_json(result)


def cmd_vault_delete(name: str):
    """Handle vault delete command."""
    result = delete_vault(name)
    output_json(result)


def cmd_vault_update(filepath: str):
    """Handle vault update command."""
    try:
        with open(filepath, "r") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        output_errors([create_error("", f"File not found: {filepath}", "parse")])
        return
    except yaml.YAMLError as e:
        output_errors([create_error("", f"Failed to parse YAML: {e}", "parse")])
        return

    if data is None:
        output_errors([create_error("", "Empty YAML file", "parse")])
        return

    result = update_vault(data)
    output_json(result)


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="python meshctl.py",
        description="Mesh Lifecycle and Topology CLI Tool"
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # mesh subcommand
    mesh_parser = subparsers.add_parser("mesh", help="Mesh resource operations")
    mesh_subparsers = mesh_parser.add_subparsers(dest="operation", help="Mesh operations")

    # mesh create
    create_parser = mesh_subparsers.add_parser("create", help="Create a mesh from YAML")
    create_parser.add_argument("-f", "--file", required=True, help="YAML file path")

    # mesh list
    mesh_subparsers.add_parser("list", help="List mesh summaries")

    # mesh describe
    describe_parser = mesh_subparsers.add_parser("describe", help="Print the full mesh")
    describe_parser.add_argument("name", help="Mesh name")

    # mesh delete
    delete_parser = mesh_subparsers.add_parser("delete", help="Delete the mesh")
    delete_parser.add_argument("name", help="Mesh name")

    # mesh update
    update_parser = mesh_subparsers.add_parser("update", help="Apply a partial update")
    update_parser.add_argument("-f", "--file", required=True, help="YAML file path")

    # vault subcommand
    vault_parser = subparsers.add_parser("vault", help="Vault resource operations")
    vault_subparsers = vault_parser.add_subparsers(dest="operation", help="Vault operations")

    # vault create
    vault_create_parser = vault_subparsers.add_parser("create", help="Create a vault from YAML")
    vault_create_parser.add_argument("-f", "--file", required=True, help="YAML file path")

    # vault list
    vault_subparsers.add_parser("list", help="List vault summaries")

    # vault describe
    vault_describe_parser = vault_subparsers.add_parser("describe", help="Print the full vault")
    vault_describe_parser.add_argument("name", help="Vault name")

    # vault delete
    vault_delete_parser = vault_subparsers.add_parser("delete", help="Delete the vault")
    vault_delete_parser.add_argument("name", help="Vault name")

    # vault update
    vault_update_parser = vault_subparsers.add_parser("update", help="Apply a partial update")
    vault_update_parser.add_argument("-f", "--file", required=True, help="YAML file path")

    args = parser.parse_args()

    if args.command == "mesh":
        if args.operation == "create":
            cmd_mesh_create(args.file)
        elif args.operation == "list":
            cmd_mesh_list()
        elif args.operation == "describe":
            cmd_mesh_describe(args.name)
        elif args.operation == "delete":
            cmd_mesh_delete(args.name)
        elif args.operation == "update":
            cmd_mesh_update(args.file)
        else:
            parser.print_help()
    elif args.command == "vault":
        if args.operation == "create":
            cmd_vault_create(args.file)
        elif args.operation == "list":
            cmd_vault_list()
        elif args.operation == "describe":
            cmd_vault_describe(args.name)
        elif args.operation == "delete":
            cmd_vault_delete(args.name)
        elif args.operation == "update":
            cmd_vault_update(args.file)
        else:
            parser.print_help()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
