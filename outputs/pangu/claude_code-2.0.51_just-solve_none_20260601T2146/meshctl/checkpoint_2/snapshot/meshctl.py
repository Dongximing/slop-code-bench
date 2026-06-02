#!/usr/bin/env python3
"""Mesh lifecycle and topology management CLI."""

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import yaml


# Data storage - persists meshes in memory
meshes_db: dict[str, "Mesh"] = {}


@dataclass
class Condition:
    type: str
    status: str  # "True" or "False"
    message: str = ""

    def to_dict(self) -> dict:
        return {"type": self.type, "status": self.status, "message": self.message}

    @classmethod
    def from_dict(cls, data: dict) -> "Condition":
        return cls(type=data["type"], status=data["status"], message=data.get("message", ""))


@dataclass
class Storage:
    size: Optional[str] = None
    ephemeral: bool = False
    className: Optional[str] = None

    def to_dict(self) -> dict:
        result = {}
        if self.ephemeral:
            result["ephemeral"] = True
        else:
            result["ephemeral"] = self.ephemeral
            result["size"] = self.size
        if self.className is not None:
            result["className"] = self.className
        return result

    @classmethod
    def from_dict(cls, data: dict) -> "Storage":
        return cls(
            size=data.get("size"),
            ephemeral=data.get("ephemeral", False),
            className=data.get("className"),
        )

    def copy(self) -> "Storage":
        return Storage(size=self.size, ephemeral=self.ephemeral, className=self.className)


@dataclass
class NetworkSpec:
    storage: Optional[Storage] = None
    replicationFactor: Optional[int] = None

    def to_dict(self) -> dict:
        result = {}
        if self.storage:
            result["storage"] = self.storage.to_dict()
        if self.replicationFactor is not None:
            result["replicationFactor"] = self.replicationFactor
        return result

    @classmethod
    def from_dict(cls, data: dict) -> "NetworkSpec":
        storage_data = data.get("storage", {})
        storage = Storage.from_dict(storage_data) if storage_data else None
        return cls(
            storage=storage,
            replicationFactor=data.get("replicationFactor"),
        )

    def copy(self) -> "NetworkSpec":
        return NetworkSpec(
            storage=self.storage.copy() if self.storage else None,
            replicationFactor=self.replicationFactor,
        )


@dataclass
class MeshSpec:
    instances: Optional[int] = None
    network: Optional[NetworkSpec] = None

    def to_dict(self) -> dict:
        result = {}
        if self.instances is not None:
            result["instances"] = self.instances
        if self.network:
            result["network"] = self.network.to_dict()
        return result

    @classmethod
    def from_dict(cls, data: dict) -> "MeshSpec":
        network_data = data.get("network", {})
        network = NetworkSpec.from_dict(network_data) if network_data else None
        return cls(
            instances=data.get("instances"),
            network=network,
        )

    def copy(self) -> "MeshSpec":
        return MeshSpec(
            instances=self.instances,
            network=self.network.copy() if self.network else None,
        )


@dataclass
class Metadata:
    name: str

    def to_dict(self) -> dict:
        return {"name": self.name}

    @classmethod
    def from_dict(cls, data: dict) -> "Metadata":
        return cls(name=data["name"])


@dataclass
class MeshStatus:
    state: str = "Running"
    stable: bool = True
    instances: Optional[dict] = None
    desiredInstancesOnResume: Optional[int] = None
    conditions: list[Condition] = field(default_factory=list)

    def to_dict(self) -> dict:
        result = {
            "state": self.state,
            "stable": self.stable,
        }
        if self.instances:
            result["instances"] = self.instances
        if self.desiredInstancesOnResume is not None:
            result["desiredInstancesOnResume"] = self.desiredInstancesOnResume
        if self.conditions:
            result["conditions"] = [c.to_dict() for c in sorted(self.conditions, key=lambda c: c.type)]
        return result

    @classmethod
    def from_dict(cls, data: dict) -> "MeshStatus":
        conditions = [Condition.from_dict(c) for c in data.get("conditions", [])]
        return cls(
            state=data.get("state", "Running"),
            stable=data.get("stable", True),
            instances=data.get("instances"),
            desiredInstancesOnResume=data.get("desiredInstancesOnResume"),
            conditions=conditions,
        )

    def copy(self) -> "MeshStatus":
        return MeshStatus(
            state=self.state,
            stable=self.stable,
            instances=self.instances.copy() if self.instances else None,
            desiredInstancesOnResume=self.desiredInstancesOnResume,
            conditions=[Condition.from_dict(c.to_dict()) for c in self.conditions],
        )


@dataclass
class Mesh:
    metadata: Metadata
    spec: MeshSpec
    status: MeshStatus

    def to_dict(self) -> dict:
        return {
            "metadata": self.metadata.to_dict(),
            "spec": self.spec.to_dict(),
            "status": self.status.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Mesh":
        metadata = Metadata.from_dict(data["metadata"])
        spec = MeshSpec.from_dict(data.get("spec", {}))
        status = MeshStatus.from_dict(data.get("status", {}))
        return cls(metadata=metadata, spec=spec, status=status)


def validate_memory_size(size: str) -> bool:
    """Validate memory quantity format."""
    if not size:
        return False
    # Non-negative integer
    if size.isdigit():
        return int(size) >= 0
    # Non-negative integer with suffix
    suffixes = ["Ki", "Mi", "Gi", "Ti"]
    for suffix in suffixes:
        if size.endswith(suffix):
            num_part = size[:-len(suffix)]
            if num_part.isdigit() and int(num_part) >= 0:
                return True
    return False


def validate_storage(storage: Storage, is_update: bool = False, existing_storage: Optional[Storage] = None) -> list[dict]:
    """Validate storage configuration. Returns list of errors."""
    errors = []

    if storage.size is None:
        # Default to "1Gi" on create when omitted
        if not is_update:
            storage.size = "1Gi"
    else:
        if not validate_memory_size(storage.size):
            errors.append({
                "field": "spec.network.storage.size",
                "type": "invalid",
                "message": f"size '{storage.size}' is not a valid memory quantity"
            })

    if is_update and existing_storage:
        # Check if size is being changed (immutable)
        if storage.size != existing_storage.size:
            errors.append({
                "field": "spec.network.storage.size",
                "type": "immutable",
                "message": f"field 'spec.network.storage.size' is immutable after creation"
            })

    return errors


def validate_replication_factor(replication_factor: Optional[int], instances: Optional[int]) -> list[dict]:
    """Validate replication factor. Returns list of errors."""
    errors = []

    if replication_factor is None:
        return errors

    if replication_factor < 1:
        errors.append({
            "field": "spec.network.replicationFactor",
            "type": "invalid",
            "message": f"replication factor {replication_factor} must be at least 1"
        })

    # Only validate that replication factor <= instances when instances > 0
    if instances is not None and instances > 0 and replication_factor > instances:
        errors.append({
            "field": "spec.network.replicationFactor",
            "type": "invalid",
            "message": f"replication factor {replication_factor} must not exceed {instances} instances"
        })

    return errors


def compute_replication_default(instances: int) -> int:
    """Compute default replication factor based on instance count."""
    if instances <= 1:
        return 1
    return min(3, instances)


def get_default_conditions() -> list[Condition]:
    """Get default conditions for a new mesh."""
    return [
        Condition(type="Healthy", status="True", message=""),
        Condition(type="PrechecksPassed", status="True", message=""),
    ]


def create_mesh(yaml_path: str) -> tuple[Optional[dict], Optional[dict]]:
    """Create a mesh from YAML file."""
    try:
        with open(yaml_path, 'r') as f:
            data = yaml.safe_load(f)
    except Exception as e:
        return None, {"errors": [{"field": "yaml", "type": "invalid", "message": str(e)}]}

    if not data or "metadata" not in data or "name" not in data["metadata"]:
        return None, {"errors": [{"field": "metadata.name", "type": "invalid", "message": "metadata.name is required"}]}

    name = data["metadata"]["name"]

    if name in meshes_db:
        return None, {"errors": [{"field": "metadata.name", "type": "exists", "message": f"mesh '{name}' already exists"}]}

    metadata = Metadata.from_dict(data["metadata"])
    spec = MeshSpec.from_dict(data.get("spec", {}))

    # Apply create-time defaults
    if spec.network is None:
        spec.network = NetworkSpec()

    # Default storage size to "1Gi" when omitted
    if spec.network.storage is None:
        spec.network.storage = Storage()
    if spec.network.storage.size is None:
        spec.network.storage.size = "1Gi"

    # Compute default replication factor
    if spec.instances is not None and spec.network.replicationFactor is None:
        spec.network.replicationFactor = compute_replication_default(spec.instances)

    # Validate
    errors = []

    # Validate storage
    errors.extend(validate_storage(spec.network.storage))

    # Validate replication factor
    errors.extend(validate_replication_factor(spec.network.replicationFactor, spec.instances))

    if errors:
        return None, {"errors": errors}

    # Determine initial state and instances
    instances = spec.instances if spec.instances is not None else 0

    if instances > 0:
        state = "Running"
        instances_status = {"ready": instances, "starting": 0, "stopped": 0}
        stable = True
    else:
        state = "Stopped"
        instances_status = {"ready": 0, "starting": 0, "stopped": 0}
        stable = True

    status = MeshStatus(
        state=state,
        stable=stable,
        instances=instances_status,
        conditions=get_default_conditions(),
    )

    mesh = Mesh(metadata=metadata, spec=spec, status=status)
    meshes_db[name] = mesh

    return mesh.to_dict(), None


def list_meshes() -> dict:
    """List mesh summaries."""
    meshes = []
    for name, mesh in meshes_db.items():
        meshes.append({
            "metadata": mesh.metadata.to_dict(),
            "spec": mesh.spec.to_dict(),
        })
    return {"items": meshes}


def describe_mesh(name: str) -> tuple[Optional[dict], Optional[dict]]:
    """Describe a specific mesh."""
    if name not in meshes_db:
        return None, {"errors": [{"field": "metadata.name", "type": "not_found", "message": f"mesh '{name}' not found"}]}

    mesh = meshes_db[name]
    mesh_dict = mesh.to_dict()

    # Auto-complete transient scaling states on describe
    status = mesh_dict.get('status', {})
    instances = status.get('instances', {}).copy()  # Get a copy
    conditions = status.get('conditions', [])

    # Check for Scaling condition - if present, auto-complete
    scaling_condition = None
    for c in conditions:
        if isinstance(c, dict) and c.get('type') == 'Scaling':
            scaling_condition = c
            break
        elif isinstance(c, Condition) and c.type == 'Scaling':
            scaling_condition = c.to_dict()
            break

    if scaling_condition:
        # Get the scaling message to determine if it's scale-up or scale-down
        message = scaling_condition.get('message', '')
        starting = instances.get('starting', 0)
        ready = instances.get('ready', 0)

        if 'Scaling down' in message:
            # Scale down: ready stays as target, starting becomes 0
            pass
        else:
            # Scale up: ready increases, starting becomes 0
            instances['ready'] = ready + starting

        instances['starting'] = 0
        status['instances'] = instances
        # Remove Scaling condition
        status['conditions'] = [
            c for c in conditions
            if not (isinstance(c, dict) and c.get('type') == 'Scaling')
            and not (isinstance(c, Condition) and c.type == 'Scaling')
        ]
        status['stable'] = True
        mesh_dict['status'] = status

    return mesh_dict, None


def delete_mesh(name: str) -> tuple[Optional[dict], Optional[dict]]:
    """Delete a mesh."""
    if name not in meshes_db:
        return None, {"errors": [{"field": "metadata.name", "type": "not_found", "message": f"mesh '{name}' not found"}]}

    del meshes_db[name]
    return {"success": True}, None


def merge_storage(updated: Storage, existing: Storage) -> Storage:
    """Merge updated storage into existing storage."""
    result = existing.copy()

    # A provided leaf field replaces the stored leaf field
    # Omitted fields keep stored values
    if updated.size is not None:
        result.size = updated.size
    if updated.ephemeral:
        result.ephemeral = updated.ephemeral
    if updated.className is not None:
        result.className = updated.className

    return result


def merge_network_spec(updated: NetworkSpec, existing: NetworkSpec) -> NetworkSpec:
    """Merge updated network spec into existing network spec."""
    result = existing.copy()

    if updated.storage is not None:
        if result.storage is None:
            result.storage = Storage()
        result.storage = merge_storage(updated.storage, result.storage)

    if updated.replicationFactor is not None:
        result.replicationFactor = updated.replicationFactor

    return result


def merge_spec(updated: MeshSpec, existing: MeshSpec) -> tuple[MeshSpec, list[dict]]:
    """Merge updated spec into existing spec. Returns merged spec and errors."""
    errors = []
    result = existing.copy()

    # Merge instances
    if updated.instances is not None:
        result.instances = updated.instances

    # Merge network
    if updated.network is not None:
        if result.network is None:
            result.network = NetworkSpec()
        result.network = merge_network_spec(updated.network, result.network)

    return result, errors


def handle_instance_scale_up(mesh: Mesh, old_instances: int, new_instances: int) -> MeshStatus:
    """Handle scale up operation. Returns transient status."""
    # In the update response:
    # status.instances.ready = Previous count
    # status.instances.starting = New count minus old count
    # Add Scaling condition with status = "True" and a non-empty message

    conditions = [c for c in mesh.status.conditions if c.type != "Scaling"]
    conditions.append(Condition(type="Scaling", status="True", message=f"Scaling from {old_instances} to {new_instances} instances"))

    return MeshStatus(
        state=mesh.status.state,
        stable=False,
        instances={
            "ready": old_instances,
            "starting": new_instances - old_instances,
            "stopped": mesh.status.instances.get("stopped", 0) if mesh.status.instances else 0,
        },
        desiredInstancesOnResume=mesh.status.desiredInstancesOnResume,
        conditions=conditions,
    )


def handle_instance_scale_down(mesh: Mesh, old_instances: int, new_instances: int) -> MeshStatus:
    """Handle scale down operation. Returns transient status."""
    # Add Scaling condition with status = "True"
    conditions = [c for c in mesh.status.conditions if c.type != "Scaling"]
    conditions.append(Condition(type="Scaling", status="True", message=f"Scaling down from {old_instances} to {new_instances} instances"))

    return MeshStatus(
        state=mesh.status.state,
        stable=False,
        instances={
            "ready": new_instances,
            "starting": old_instances - new_instances,
            "stopped": mesh.status.instances.get("stopped", 0) if mesh.status.instances else 0,
        },
        desiredInstancesOnResume=mesh.status.desiredInstancesOnResume,
        conditions=conditions,
    )


def handle_stop(mesh: Mesh, old_instances: int) -> MeshStatus:
    """Handle stop operation (instances -> 0)."""
    # Add GracefulShutdown with status = "True" and message = ""
    # Set status.desiredInstancesOnResume to previous instance count
    # Set status.instances: ready=0, starting=0, stopped=Previous count
    # Set status.state = "Stopped"

    conditions = [c for c in mesh.status.conditions if c.type != "GracefulShutdown"]
    conditions.append(Condition(type="GracefulShutdown", status="True", message=""))

    return MeshStatus(
        state="Stopped",
        stable=True,
        instances={
            "ready": 0,
            "starting": 0,
            "stopped": old_instances,
        },
        desiredInstancesOnResume=old_instances,
        conditions=conditions,
    )


def handle_resume(mesh: Mesh, target_instances: int) -> MeshStatus:
    """Handle resume operation. Returns transient status."""
    # Remove GracefulShutdown
    # Remove status.desiredInstancesOnResume
    # If spec.instances is positive, use it as target count
    # If spec.instances is omitted or null, use status.desiredInstancesOnResume
    # In update response: ready=0, starting=target, stopped=0, state="Running"

    conditions = [c for c in mesh.status.conditions if c.type != "GracefulShutdown"]

    return MeshStatus(
        state="Running",
        stable=False,
        instances={
            "ready": 0,
            "starting": target_instances,
            "stopped": 0,
        },
        desiredInstancesOnResume=None,
        conditions=conditions,
    )


def update_mesh(yaml_path: str) -> tuple[Optional[dict], Optional[dict]]:
    """Apply a partial update to a mesh."""
    try:
        with open(yaml_path, 'r') as f:
            data = yaml.safe_load(f)
    except Exception as e:
        return None, {"errors": [{"field": "yaml", "type": "invalid", "message": str(e)}]}

    if not data or "metadata" not in data or "name" not in data["metadata"]:
        return None, {"errors": [{"field": "metadata.name", "type": "invalid", "message": "metadata.name is required"}]}

    name = data["metadata"]["name"]

    if name not in meshes_db:
        return None, {"errors": [{"field": "metadata.name", "type": "not_found", "message": f"mesh '{name}' not found"}]}

    existing_mesh = meshes_db[name]

    # Parse the update data
    updated_spec = MeshSpec.from_dict(data.get("spec", {}))

    # Check for immutable field changes before merging
    errors = []

    # Check storage size immutability
    if updated_spec.network and updated_spec.network.storage and updated_spec.network.storage.size:
        if existing_mesh.spec.network and existing_mesh.spec.network.storage:
            if updated_spec.network.storage.size != existing_mesh.spec.network.storage.size:
                errors.append({
                    "field": "spec.network.storage.size",
                    "type": "immutable",
                    "message": "field 'spec.network.storage.size' is immutable after creation"
                })

    # If there are errors, reject the whole update
    if errors:
        return None, {"errors": errors}

    # Store old values for lifecycle handling
    old_instances = existing_mesh.spec.instances if existing_mesh.spec.instances is not None else 0

    # Perform the merge
    merged_spec, merge_errors = merge_spec(updated_spec, existing_mesh.spec)

    if merge_errors:
        return None, {"errors": merge_errors}

    # Validate the merged spec
    errors = []

    # Validate storage
    if merged_spec.network and merged_spec.network.storage:
        errors.extend(validate_storage(merged_spec.network.storage, is_update=True,
                                       existing_storage=existing_mesh.spec.network.storage if existing_mesh.spec.network else None))

    # Validate replication factor
    errors.extend(validate_replication_factor(merged_spec.network.replicationFactor if merged_spec.network else None,
                                              merged_spec.instances))

    # If there are errors, reject the whole update
    if errors:
        return None, {"errors": errors}

    # Determine new instance count
    new_instances = merged_spec.instances if merged_spec.instances is not None else old_instances

    # Compute default replication factor if needed
    if new_instances > 0 and (merged_spec.network is None or merged_spec.network.replicationFactor is None):
        if merged_spec.network is None:
            merged_spec.network = NetworkSpec()
        merged_spec.network.replicationFactor = compute_replication_default(new_instances)

    # Handle instance lifecycle transitions
    new_status = existing_mesh.status.copy()

    if old_instances > 0 and new_instances == 0:
        # Stop
        new_status = handle_stop(existing_mesh, old_instances)
    elif old_instances == 0 and new_instances > 0:
        # Resume - check if GracefulShutdown exists
        if any(c.type == "GracefulShutdown" for c in existing_mesh.status.conditions):
            # Use spec.instances if provided, otherwise use desiredInstancesOnResume
            target = new_instances if new_instances > 0 else (existing_mesh.status.desiredInstancesOnResume or 0)
            new_status = handle_resume(existing_mesh, target)
    elif new_instances > old_instances:
        # Scale up
        new_status = handle_instance_scale_up(existing_mesh, old_instances, new_instances)
    elif new_instances < old_instances:
        # Scale down
        new_status = handle_instance_scale_down(existing_mesh, old_instances, new_instances)
    else:
        # No change in instances - update is still valid
        # Just update conditions if needed (e.g., clearing Scaling)
        pass

    # Update the mesh
    updated_mesh = Mesh(
        metadata=existing_mesh.metadata,
        spec=merged_spec,
        status=new_status,
    )
    meshes_db[name] = updated_mesh

    return updated_mesh.to_dict(), None


def main():
    parser = argparse.ArgumentParser(description="Mesh lifecycle management")
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Mesh commands
    mesh_parser = subparsers.add_parser("mesh", help="Mesh operations")
    mesh_subparsers = mesh_parser.add_subparsers(dest="mesh_command", help="Mesh operations")

    # mesh create
    create_parser = mesh_subparsers.add_parser("create", help="Create a mesh")
    create_parser.add_argument("-f", "--file", required=True, help="YAML file path")

    # mesh list
    list_parser = mesh_subparsers.add_parser("list", help="List meshes")

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

    result = None
    error = None

    if args.mesh_command == "create":
        result, error = create_mesh(args.file)
    elif args.mesh_command == "list":
        result = list_meshes()
    elif args.mesh_command == "describe":
        result, error = describe_mesh(args.name)
    elif args.mesh_command == "delete":
        result, error = delete_mesh(args.name)
    elif args.mesh_command == "update":
        result, error = update_mesh(args.file)

    if error:
        print(json.dumps(error, indent=2))
        sys.exit(1)
    else:
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
