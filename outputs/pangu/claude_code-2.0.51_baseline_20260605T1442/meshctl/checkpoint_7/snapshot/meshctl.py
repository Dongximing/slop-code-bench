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

# Data storage for tasks, snapshots, and recoveries
_TASKS: Dict[str, Dict] = {}
_SNAPSHOTS: Dict[str, Dict] = {}
_RECOVERIES: Dict[str, Dict] = {}


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


def load_tasks_storage() -> Dict[str, Dict]:
    """Load tasks from persistent storage."""
    storage_file = "/tmp/tasks.json"
    if os.path.exists(storage_file):
        with open(storage_file, "r") as f:
            return json.load(f)
    return {}


def save_tasks_storage(data: Dict):
    """Save tasks to persistent storage."""
    storage_file = "/tmp/tasks.json"
    with open(storage_file, "w") as f:
        json.dump(data, f, indent=2)


def load_snapshots_storage() -> Dict[str, Dict]:
    """Load snapshots from persistent storage."""
    storage_file = "/tmp/snapshots.json"
    if os.path.exists(storage_file):
        with open(storage_file, "r") as f:
            return json.load(f)
    return {}


def save_snapshots_storage(data: Dict):
    """Save snapshots to persistent storage."""
    storage_file = "/tmp/snapshots.json"
    with open(storage_file, "w") as f:
        json.dump(data, f, indent=2)


def load_recoveries_storage() -> Dict[str, Dict]:
    """Load recoveries from persistent storage."""
    storage_file = "/tmp/recoveries.json"
    if os.path.exists(storage_file):
        with open(storage_file, "r") as f:
            return json.load(f)
    return {}


def save_recoveries_storage(data: Dict):
    """Save recoveries to persistent storage."""
    storage_file = "/tmp/recoveries.json"
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

    # Validate exposure
    exposure = spec.get("exposure")
    errors.extend(validate_exposure(exposure))

    # Validate management
    management = spec.get("management")
    errors.extend(validate_management(management, update, existing))

    # Validate security (access)
    access = spec.get("access")
    errors.extend(validate_security(access))

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

    # Validate security (access)
    access = spec.get("access")
    errors.extend(validate_security(access))

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


# Exposure and Connectivity Validation

VALID_EXPOSURE_TYPES = {"Gateway", "DirectPort", "Balancer"}
GATEWAY_ONLY_FIELDS = {"hostname", "annotations"}
DIRECTPORT_ONLY_FIELDS = {"port", "directPort"}
BALANCER_ONLY_FIELDS = {"port"}


def validate_exposure(exposure: Any) -> List[Dict[str, str]]:
    """Validate exposure configuration."""
    errors = []

    if exposure is None:
        return errors

    exposure_type = exposure.get("type")

    # Validate exposure.type is present and valid
    if not exposure_type or not isinstance(exposure_type, str):
        errors.append(create_error("spec.exposure.type", "spec.exposure.type is required", "required"))
        return errors

    if exposure_type not in VALID_EXPOSURE_TYPES:
        errors.append(create_error("spec.exposure.type", f"'{exposure_type}' is not a valid exposure type", "invalid"))
        return errors

    # Collect allowed fields based on type
    if exposure_type == "Gateway":
        allowed_fields = GATEWAY_ONLY_FIELDS
    elif exposure_type == "DirectPort":
        allowed_fields = DIRECTPORT_ONLY_FIELDS
    else:  # Balancer
        allowed_fields = BALANCER_ONLY_FIELDS

    # Check for forbidden fields
    for field in exposure:
        if field not in allowed_fields and field != "type":
            errors.append(create_error(f"spec.exposure.{field}", f"field 'spec.exposure.{field}' is not allowed for exposure type '{exposure_type}'", "forbidden"))

    # Validate field types and values
    if exposure_type == "Gateway":
        hostname = exposure.get("hostname")
        if hostname is not None and not isinstance(hostname, str):
            errors.append(create_error("spec.exposure.hostname", "hostname must be a string", "invalid"))

        annotations = exposure.get("annotations")
        if annotations is not None:
            if not isinstance(annotations, dict):
                errors.append(create_error("spec.exposure.annotations", "annotations must be a map", "invalid"))

    elif exposure_type == "DirectPort":
        port = exposure.get("port")
        if port is not None:
            if not isinstance(port, int):
                errors.append(create_error("spec.exposure.port", "port must be an integer", "invalid"))
            elif port < 1 or port > 65535:
                errors.append(create_error("spec.exposure.port", "port must be between 1 and 65535", "invalid"))

        direct_port = exposure.get("directPort")
        if direct_port is not None:
            if not isinstance(direct_port, int):
                errors.append(create_error("spec.exposure.directPort", "directPort must be an integer", "invalid"))
            elif direct_port < 1 or direct_port > 65535:
                errors.append(create_error("spec.exposure.directPort", "directPort must be between 1 and 65535", "invalid"))

    else:  # Balancer
        port = exposure.get("port")
        if port is not None:
            if not isinstance(port, int):
                errors.append(create_error("spec.exposure.port", "port must be an integer", "invalid"))
            elif port < 1 or port > 65535:
                errors.append(create_error("spec.exposure.port", "port must be between 1 and 65535", "invalid"))

    return errors


def compute_connection_details(mesh_name: str, exposure: Dict) -> Dict:
    """Compute connection details based on exposure configuration."""
    if exposure is None:
        return None

    exposure_type = exposure.get("type")

    if exposure_type == "Gateway":
        hostname = exposure.get("hostname")
        host = hostname if hostname else f"{mesh_name}.example.com"
        return {
            "host": host,
            "port": 443,
            "protocol": "https"
        }
    elif exposure_type == "DirectPort":
        direct_port = exposure.get("directPort")
        port = direct_port if direct_port is not None else 8443
        return {
            "host": mesh_name,
            "port": port,
            "protocol": "https"
        }
    elif exposure_type == "Balancer":
        port = exposure.get("port")
        port_val = port if port is not None else 80
        return {
            "host": f"{mesh_name}-external",
            "port": port_val,
            "protocol": "https"
        }

    return None


def compute_management_connection_details(mesh_name: str) -> Dict:
    """Compute management connection details."""
    return {
        "host": f"{mesh_name}-admin",
        "port": 9990,
        "protocol": "https"
    }


def validate_management(management: Any, is_update: bool, existing: Optional[Dict]) -> List[Dict[str, str]]:
    """Validate management configuration."""
    errors = []

    if management is None:
        return errors

    enabled = management.get("enabled")
    if enabled is None:
        return errors

    if not isinstance(enabled, bool):
        errors.append(create_error("spec.management.enabled", "enabled must be a boolean", "invalid"))
        return errors

    # Check immutability on update
    if is_update and existing:
        existing_spec = existing.get("spec", {})
        existing_management = existing_spec.get("management", {})
        existing_enabled = existing_management.get("enabled")

        # If existing had a value and it's different, it's immutable
        if existing_enabled is not None and existing_enabled != enabled:
            errors.append(create_error(
                "spec.management.enabled",
                "field 'spec.management.enabled' is immutable after creation",
                "immutable"
            ))

    return errors


# Security Model Validation

def validate_authentication(auth: Any, enabled: bool) -> List[Dict[str, str]]:
    """Validate authentication settings according to the security model."""
    errors = []

    if auth is None:
        return errors

    if not enabled:
        # Authentication is disabled
        digest_algorithm = auth.get("digestAlgorithm")
        if digest_algorithm is not None:
            errors.append(create_error(
                "spec.access.authentication.digestAlgorithm",
                f"digestAlgorithm '{digest_algorithm}' is forbidden when authentication is disabled",
                "forbidden"
            ))
        credential_ref = auth.get("credentialRef")
        if credential_ref is not None:
            errors.append(create_error(
                "spec.access.credentialRef",
                f"credentialRef '{credential_ref}' is forbidden when authentication is disabled",
                "forbidden"
            ))
    else:
        # Authentication is enabled - validate digestAlgorithm
        digest_algorithm = auth.get("digestAlgorithm")
        if digest_algorithm is not None:
            valid_algorithms = ("SHA-256", "SHA-384", "SHA-512")
            if digest_algorithm not in valid_algorithms:
                errors.append(create_error(
                    "spec.access.authentication.digestAlgorithm",
                    f"'{digest_algorithm}' is not a valid digestAlgorithm, must be one of {valid_algorithms}",
                    "invalid"
                ))

    return errors


def validate_roles(roles: Any) -> List[Dict[str, str]]:
    """Validate role shapes and check for duplicate names."""
    errors = []

    if roles is None:
        return errors

    if not isinstance(roles, list):
        errors.append(create_error(
            "spec.access.permissions.roles",
            "roles must be an array",
            "invalid"
        ))
        return errors

    seen_names = set()
    for idx, role in enumerate(roles):
        if not isinstance(role, dict):
            errors.append(create_error(
                f"spec.access.permissions.roles[{idx}]",
                "role must be an object",
                "invalid"
            ))
            continue

        # Validate name
        name = role.get("name")
        if not name or not isinstance(name, str):
            errors.append(create_error(
                f"spec.access.permissions.roles[{idx}].name",
                "name is required and must be non-empty"
            ))

        # Validate permissions
        permissions = role.get("permissions")
        if not permissions or not isinstance(permissions, list) or len(permissions) == 0:
            errors.append(create_error(
                f"spec.access.permissions.roles[{idx}].permissions",
                "permissions is required and must be a non-empty array"
            ))

        # Check for duplicate names
        if name and isinstance(name, str):
            if name in seen_names:
                errors.append(create_error(
                    "spec.access.permissions.roles",
                    f"duplicate role name '{name}'",
                    "duplicate"
                ))
            seen_names.add(name)

    return errors


def validate_permissions(permissions: Any) -> List[Dict[str, str]]:
    """Validate permissions settings according to the security model."""
    errors = []

    if permissions is None:
        return errors

    enabled = permissions.get("enabled", False)

    # When permissions are enabled, roles must be present and have at least one entry
    if enabled:
        roles = permissions.get("roles")
        if not roles or not isinstance(roles, list) or len(roles) == 0:
            errors.append(create_error(
                "spec.access.permissions.roles",
                "roles is required when permissions are enabled and must contain at least one entry",
                "required"
            ))
        else:
            # Validate roles
            errors.extend(validate_roles(roles))

    return errors


def validate_encryption(encryption: Any) -> List[Dict[str, str]]:
    """Validate encryption settings according to the security model."""
    errors = []

    if encryption is None:
        return errors

    # Validate source
    source = encryption.get("source", "None")
    valid_sources = ("None", "Secret", "Service")
    if source not in valid_sources:
        errors.append(create_error(
            "spec.access.encryption.source",
            f"'{source}' is not a valid source, must be one of {valid_sources}",
            "invalid"
        ))
    else:
        # Validate based on source value
        cert_ref = encryption.get("certRef")
        cert_service_ref = encryption.get("certServiceRef")

        if source == "None":
            # certRef and certServiceRef are both forbidden
            if cert_ref is not None:
                errors.append(create_error(
                    "spec.access.encryption.certRef",
                    "certRef is forbidden when source is 'None'",
                    "forbidden"
                ))
            if cert_service_ref is not None:
                errors.append(create_error(
                    "spec.access.encryption.certServiceRef",
                    "certServiceRef is forbidden when source is 'None'",
                    "forbidden"
                ))
            # clientMode must be "None" when source is "None"
            client_mode = encryption.get("clientMode", "None")
            if client_mode not in ("None",):
                errors.append(create_error(
                    "spec.access.encryption.clientMode",
                    f"clientMode must be 'None' when source is 'None', got '{client_mode}'",
                    "invalid"
                ))

        elif source == "Secret":
            # certRef is required, certServiceRef is forbidden
            if cert_ref is None:
                errors.append(create_error(
                    "spec.access.encryption.certRef",
                    "certRef is required when source is 'Secret'",
                    "required"
                ))
            if cert_service_ref is not None:
                errors.append(create_error(
                    "spec.access.encryption.certServiceRef",
                    "certServiceRef is forbidden when source is 'Secret'",
                    "forbidden"
                ))

        elif source == "Service":
            # certRef is forbidden, certServiceRef is required
            if cert_ref is not None:
                errors.append(create_error(
                    "spec.access.encryption.certRef",
                    "certRef is forbidden when source is 'Service'",
                    "forbidden"
                ))
            if cert_service_ref is None:
                errors.append(create_error(
                    "spec.access.encryption.certServiceRef",
                    "certServiceRef is required when source is 'Service'",
                    "required"
                ))

    # Validate clientMode if present
    client_mode = encryption.get("clientMode")
    if client_mode is not None:
        valid_client_modes = ("None", "Authenticate", "Validate")
        if client_mode not in valid_client_modes:
            errors.append(create_error(
                "spec.access.encryption.clientMode",
                f"'{client_mode}' is not a valid clientMode, must be one of {valid_client_modes}",
                "invalid"
            ))

    return errors


def validate_security(access: Any) -> List[Dict[str, str]]:
    """Validate the full security (access) configuration."""
    errors = []

    if access is None:
        return errors

    # Validate authentication
    auth = access.get("authentication")
    auth_enabled = auth.get("enabled", True) if auth else True
    errors.extend(validate_authentication(auth, auth_enabled))

    # Validate permissions
    permissions = access.get("permissions")
    errors.extend(validate_permissions(permissions))

    # Validate encryption
    encryption = access.get("encryption")
    errors.extend(validate_encryption(encryption))

    return errors


def apply_security_defaults(access: Any, is_update: bool = False) -> Dict:
    """Apply default values to security configuration."""
    if access is None:
        access = {}

    # Authentication defaults
    auth = access.get("authentication", {})
    if "enabled" not in auth:
        auth["enabled"] = True
    if auth.get("enabled", True) and "digestAlgorithm" not in auth:
        auth["digestAlgorithm"] = "SHA-256"
    access["authentication"] = auth

    # Encryption defaults
    encryption = access.get("encryption", {})
    if "source" not in encryption:
        encryption["source"] = "None"
    if "clientMode" not in encryption:
        encryption["clientMode"] = "None"
    access["encryption"] = encryption

    # Permissions defaults
    permissions = access.get("permissions", {})
    if "enabled" not in permissions:
        permissions["enabled"] = False
    access["permissions"] = permissions

    return access


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

    # Apply security defaults
    access = spec.get("access")
    spec["access"] = apply_security_defaults(access)

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

    # Add connection details if exposure is configured
    exposure = spec.get("exposure")
    if exposure is not None:
        mesh["status"]["connectionDetails"] = compute_connection_details(name, exposure)

    # Add management connection details if management is enabled
    management = spec.get("management", {})
    if management.get("enabled", False):
        mesh["status"]["managementConnectionDetails"] = compute_management_connection_details(name)

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

    # Update connection details based on exposure
    status = updated.get("status", {})
    spec = updated.get("spec", {})
    exposure = spec.get("exposure")

    # Remove connectionDetails if exposure is removed
    if exposure is None:
        status.pop("connectionDetails", None)
    else:
        status["connectionDetails"] = compute_connection_details(name, exposure)

    # Update management connection details
    management = spec.get("management", {})
    if management.get("enabled", False):
        status["managementConnectionDetails"] = compute_management_connection_details(name)
    else:
        status.pop("managementConnectionDetails", None)

    updated["status"] = status

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

    # Apply security defaults
    access = spec.get("access")
    spec["access"] = apply_security_defaults(access)

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


# Task Validation and Functions

def validate_task(data: Dict, update: bool = False, existing: Optional[Dict] = None) -> List[Dict[str, str]]:
    """Validate task data."""
    errors = []

    # Validate metadata.name
    metadata = data.get("metadata", {})
    name = metadata.get("name")
    if not name or not isinstance(name, str):
        errors.append(create_error("metadata.name", "metadata.name is required", "required"))
        return errors

    if not update and name:
        storage = load_tasks_storage()
        if name in storage:
            errors.append(create_error("metadata.name", f"task '{name}' already exists", "duplicate"))
            return errors

    spec = data.get("spec", {})

    # Validate spec.meshRef (required)
    mesh_ref = spec.get("meshRef")
    if not mesh_ref or not isinstance(mesh_ref, str):
        errors.append(create_error("spec.meshRef", "spec.meshRef is required", "required"))
        return errors

    # Check if mesh exists
    meshes_storage = load_meshes_storage()
    if mesh_ref not in meshes_storage:
        errors.append(create_error("spec.meshRef", f'mesh "{mesh_ref}" not found', "invalid"))
        return errors

    # Validate inline/bundleRef exclusivity
    has_inline = spec.get("inline") is not None
    has_bundle_ref = spec.get("bundleRef") is not None

    if not has_inline and not has_bundle_ref:
        errors.append(create_error("spec", "exactly one of 'spec.inline' or 'spec.bundleRef' must be set", "invalid"))
    elif has_inline and has_bundle_ref:
        errors.append(create_error("spec", "exactly one of 'spec.inline' or 'spec.bundleRef' must be set", "invalid"))
    elif has_inline:
        inline_val = spec.get("inline")
        if not inline_val or not isinstance(inline_val, str):
            errors.append(create_error("spec.inline", "spec.inline cannot be empty", "invalid"))
    elif has_bundle_ref:
        bundle_ref_val = spec.get("bundleRef")
        if not bundle_ref_val or not isinstance(bundle_ref_val, str):
            errors.append(create_error("spec.bundleRef", "spec.bundleRef cannot be empty", "invalid"))

    # Check for spec immutability on update
    if update and existing:
        existing_spec = existing.get("spec", {})
        if existing_spec != spec:
            errors.append(create_error("spec", "spec is immutable after creation", "immutable"))

    return errors


def create_task(data: Dict) -> Dict:
    """Create a new task."""
    errors = validate_task(data, update=False)
    if errors:
        return {"errors": errors}

    name = data["metadata"]["name"]
    spec = data.get("spec", {})

    task = {
        "apiVersion": "mesh.example.com/v1",
        "kind": "Task",
        "metadata": data.get("metadata", {}),
        "spec": spec,
        "status": {
            "state": "Initializing"
        }
    }

    storage = load_tasks_storage()
    storage[name] = task
    save_tasks_storage(storage)

    return task


def list_tasks() -> List[Dict]:
    """List all task summaries sorted by name."""
    storage = load_tasks_storage()
    result = []

    for name in sorted(storage.keys()):
        task = storage[name]
        summary = {
            "metadata": {
                "name": name
            },
            "spec": {
                "meshRef": task.get("spec", {}).get("meshRef", "")
            },
            "status": {
                "state": task.get("status", {}).get("state", "Unknown")
            }
        }
        result.append(summary)

    return result


def describe_task(name: str) -> Dict:
    """Describe a specific task."""
    storage = load_tasks_storage()

    if name not in storage:
        return {"errors": [create_error("metadata.name", f'task "{name}" not found', "not_found")]}

    return storage[name]


def update_task(data: Dict) -> Dict:
    """Update an existing task."""
    task_name = data.get("metadata", {}).get("name")

    if not task_name:
        return {"errors": [create_error("metadata.name", "metadata.name is required for update", "required")]}

    storage = load_tasks_storage()
    if task_name not in storage:
        return {"errors": [create_error("metadata.name", f'task "{task_name}" not found', "not_found")]}

    existing = storage[task_name]

    errors = validate_task(data, update=True, existing=existing)
    if errors:
        return {"errors": errors}

    # Spec is immutable, so no update should happen
    return {"errors": [create_error("spec", "spec is immutable after creation", "immutable")]}


def delete_task(name: str) -> Dict:
    """Delete a task."""
    storage = load_tasks_storage()

    if name not in storage:
        return {"errors": [create_error("metadata.name", f'task "{name}" not found', "not_found")]}

    del storage[name]
    save_tasks_storage(storage)

    return {
        "message": f"Task '{name}' has been deleted",
        "metadata": {"name": name}
    }


def run_task(name: str) -> Dict:
    """Execute a task."""
    storage = load_tasks_storage()

    if name not in storage:
        return {"errors": [create_error("metadata.name", f'task "{name}" not found', "not_found")]}

    task = storage[name]
    status = task.get("status", {})
    current_state = status.get("state", "Unknown")

    # Run is valid only from "Initializing"
    if current_state != "Initializing":
        return {"errors": [create_error(
            "status.state",
            f"resource is in state '{current_state}', expected 'Initializing'",
            "invalid"
        )]}

    # Transition to Running
    task["status"]["state"] = "Running"
    storage[name] = task
    save_tasks_storage(storage)

    # Execute the task
    spec = task.get("spec", {})
    inline_content = spec.get("inline")

    if inline_content:
        lines = inline_content.strip().split("\n")
        failed = False
        fail_reason = ""
        fail_index = -1

        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("FAIL:"):
                failed = True
                fail_reason = stripped[5:].strip() or "command failed"
                fail_index = i
                break

        if failed:
            task["status"]["state"] = "Failed"
            task["status"]["detail"] = f"command {fail_index} failed: {fail_reason}"
        else:
            task["status"]["state"] = "Succeeded"
    else:
        # bundleRef case - success
        task["status"]["state"] = "Succeeded"

    storage[name] = task
    save_tasks_storage(storage)

    return task


# Snapshot Validation and Functions

def validate_snapshot(data: Dict, update: bool = False, existing: Optional[Dict] = None) -> List[Dict[str, str]]:
    """Validate snapshot data."""
    errors = []

    # Validate metadata.name
    metadata = data.get("metadata", {})
    name = metadata.get("name")
    if not name or not isinstance(name, str):
        errors.append(create_error("metadata.name", "metadata.name is required", "required"))
        return errors

    if not update and name:
        storage = load_snapshots_storage()
        if name in storage:
            errors.append(create_error("metadata.name", f"snapshot '{name}' already exists", "duplicate"))
            return errors

    spec = data.get("spec", {})

    # Validate spec.meshRef (required)
    mesh_ref = spec.get("meshRef")
    if not mesh_ref or not isinstance(mesh_ref, str):
        errors.append(create_error("spec.meshRef", "spec.meshRef is required", "required"))
        return errors

    # Check if mesh exists
    meshes_storage = load_meshes_storage()
    if mesh_ref not in meshes_storage:
        errors.append(create_error("spec.meshRef", f'mesh "{mesh_ref}" not found', "invalid"))
        return errors

    # Validate storage.size and storage.className if present
    storage_spec = spec.get("storage", {})
    if storage_spec:
        size = storage_spec.get("size")
        if size is not None:
            if not isinstance(size, str):
                errors.append(create_error("spec.storage.size", "size must be a string", "invalid"))
            elif not parse_memory_quantity(size):
                errors.append(create_error("spec.storage.size", f"'{size}' is not a valid memory quantity", "invalid"))

        class_name = storage_spec.get("className")
        if class_name is not None and not isinstance(class_name, str):
            errors.append(create_error("spec.storage.className", "className must be a string", "invalid"))

    # Validate scope if present
    scope = spec.get("scope")
    if scope is not None:
        if not isinstance(scope, dict):
            errors.append(create_error("spec.scope", "scope must be an object", "invalid"))
        else:
            valid_keys = {"stores", "blueprints", "tallies", "definitions", "procedures"}
            for key in scope.keys():
                if key not in valid_keys:
                    errors.append(create_error(f"spec.scope.{key}", f"'{key}' is not a valid scope key", "invalid"))

    # Validate resources.memory
    resources = spec.get("resources", {})
    if resources:
        memory = resources.get("memory")
        if memory is not None:
            if not isinstance(memory, dict):
                errors.append(create_error("spec.resources.memory", "memory must be an object", "invalid"))
            else:
                for mem_key in ["limit", "request"]:
                    val = memory.get(mem_key)
                    if val is not None:
                        if not isinstance(val, str):
                            errors.append(create_error(f"spec.resources.memory.{mem_key}", f"{mem_key} must be a string", "invalid"))
                        elif not parse_memory_quantity(val):
                            errors.append(create_error(f"spec.resources.memory.{mem_key}", f"'{val}' is not a valid memory quantity", "invalid"))

        cpu = resources.get("cpu")
        if cpu is not None:
            if not isinstance(cpu, dict):
                errors.append(create_error("spec.resources.cpu", "cpu must be an object", "invalid"))

    # Check for spec immutability on update
    if update and existing:
        existing_spec = existing.get("spec", {})
        if existing_spec != spec:
            errors.append(create_error("spec", "spec is immutable after creation", "immutable"))

    return errors


def create_snapshot(data: Dict) -> Dict:
    """Create a new snapshot."""
    errors = validate_snapshot(data, update=False)
    if errors:
        return {"errors": errors}

    name = data["metadata"]["name"]
    spec = data.get("spec", {})

    # Apply default memory if not specified
    resources = spec.get("resources", {})
    if "memory" not in resources:
        resources = resources.copy()
        resources["memory"] = {"limit": "1Gi", "request": "1Gi"}
        spec = spec.copy()
        spec["resources"] = resources

    snapshot = {
        "apiVersion": "mesh.example.com/v1",
        "kind": "Snapshot",
        "metadata": data.get("metadata", {}),
        "spec": spec,
        "status": {
            "state": "Initializing"
        }
    }

    storage = load_snapshots_storage()
    storage[name] = snapshot
    save_snapshots_storage(storage)

    return snapshot


def list_snapshots() -> List[Dict]:
    """List all snapshot summaries sorted by name."""
    storage = load_snapshots_storage()
    result = []

    for name in sorted(storage.keys()):
        snapshot = storage[name]
        summary = {
            "metadata": {
                "name": name
            },
            "spec": {
                "meshRef": snapshot.get("spec", {}).get("meshRef", "")
            },
            "status": {
                "state": snapshot.get("status", {}).get("state", "Unknown")
            }
        }
        result.append(summary)

    return result


def describe_snapshot(name: str) -> Dict:
    """Describe a specific snapshot."""
    storage = load_snapshots_storage()

    if name not in storage:
        return {"errors": [create_error("metadata.name", f'snapshot "{name}" not found', "not_found")]}

    return storage[name]


def update_snapshot(data: Dict) -> Dict:
    """Update an existing snapshot."""
    snapshot_name = data.get("metadata", {}).get("name")

    if not snapshot_name:
        return {"errors": [create_error("metadata.name", "metadata.name is required for update", "required")]}

    storage = load_snapshots_storage()
    if snapshot_name not in storage:
        return {"errors": [create_error("metadata.name", f'snapshot "{snapshot_name}" not found', "not_found")]}

    existing = storage[snapshot_name]

    errors = validate_snapshot(data, update=True, existing=existing)
    if errors:
        return {"errors": errors}

    # Spec is immutable, so no update should happen
    return {"errors": [create_error("spec", "spec is immutable after creation", "immutable")]}


def delete_snapshot(name: str) -> Dict:
    """Delete a snapshot."""
    # Check for recovery dependencies first
    errors = validate_snapshot_delete_for_recoveries(name)
    if errors:
        return {"errors": errors}

    storage = load_snapshots_storage()

    if name not in storage:
        return {"errors": [create_error("metadata.name", f'snapshot "{name}" not found', "not_found")]}

    del storage[name]
    save_snapshots_storage(storage)

    return {
        "message": f"Snapshot '{name}' has been deleted",
        "metadata": {"name": name}
    }


def validate_snapshot_delete_for_recoveries(snapshot_name: str) -> List[Dict[str, str]]:
    """Check if any recoveries reference the given snapshot. Returns errors if recoveries exist."""
    errors = []
    recoveries_storage = load_recoveries_storage()
    dependent_recoveries = []

    for recovery_name, recovery in recoveries_storage.items():
        snapshot_ref = recovery.get("spec", {}).get("snapshotRef")
        if snapshot_ref == snapshot_name:
            dependent_recoveries.append(recovery.get("metadata", {}).get("name", recovery_name))

    if dependent_recoveries:
        errors.append(create_error(
            "metadata.name",
            f"snapshot '{snapshot_name}' cannot be deleted because it is referenced by recoveries: {', '.join(dependent_recoveries)}",
            "conflict"
        ))

    return errors


def run_snapshot(name: str) -> Dict:
    """Execute a snapshot."""
    storage = load_snapshots_storage()

    if name not in storage:
        return {"errors": [create_error("metadata.name", f'snapshot "{name}" not found', "not_found")]}

    snapshot = storage[name]
    status = snapshot.get("status", {})
    current_state = status.get("state", "Unknown")

    # Run is valid only from "Initializing"
    if current_state != "Initializing":
        return {"errors": [create_error(
            "status.state",
            f"resource is in state '{current_state}', expected 'Initializing'",
            "invalid"
        )]}

    # Transition to Running
    snapshot["status"]["state"] = "Running"
    storage[name] = snapshot
    save_snapshots_storage(storage)

    # Execute the snapshot
    spec = snapshot.get("spec", {})
    mesh_ref = spec.get("meshRef")

    # Check if mesh is stable
    meshes_storage = load_meshes_storage()
    mesh = meshes_storage.get(mesh_ref, {})
    mesh_stable = mesh.get("status", {}).get("stable", True)

    if not mesh_stable:
        snapshot["status"]["state"] = "Unknown"
        snapshot["status"]["detail"] = f"mesh '{mesh_ref}' is not stable"
    else:
        # Success - set a stable, non-empty storageRef
        import uuid
        snapshot["status"]["state"] = "Succeeded"
        snapshot["status"]["storageRef"] = f"snapshot-{name}-{uuid.uuid4().hex[:8]}"

    storage[name] = snapshot
    save_snapshots_storage(storage)

    return snapshot


# Recovery Validation and Functions

def validate_recovery(data: Dict, update: bool = False, existing: Optional[Dict] = None) -> List[Dict[str, str]]:
    """Validate recovery data."""
    errors = []

    # Validate metadata.name
    metadata = data.get("metadata", {})
    name = metadata.get("name")
    if not name or not isinstance(name, str):
        errors.append(create_error("metadata.name", "metadata.name is required", "required"))
        return errors

    if not update and name:
        storage = load_recoveries_storage()
        if name in storage:
            errors.append(create_error("metadata.name", f"recovery '{name}' already exists", "duplicate"))
            return errors

    spec = data.get("spec", {})

    # Validate spec.meshRef (required)
    mesh_ref = spec.get("meshRef")
    if not mesh_ref or not isinstance(mesh_ref, str):
        errors.append(create_error("spec.meshRef", "spec.meshRef is required", "required"))
        return errors

    # Check if mesh exists
    meshes_storage = load_meshes_storage()
    if mesh_ref not in meshes_storage:
        errors.append(create_error("spec.meshRef", f'mesh "{mesh_ref}" not found', "invalid"))
        return errors

    # Validate spec.snapshotRef (required)
    snapshot_ref = spec.get("snapshotRef")
    if not snapshot_ref or not isinstance(snapshot_ref, str):
        errors.append(create_error("spec.snapshotRef", "spec.snapshotRef is required", "required"))
        return errors

    # Check if snapshot exists
    snapshots_storage = load_snapshots_storage()
    if snapshot_ref not in snapshots_storage:
        errors.append(create_error("spec.snapshotRef", f'snapshot "{snapshot_ref}" not found', "invalid"))
        return errors

    # Check that snapshot's meshRef matches recovery's meshRef
    snapshot = snapshots_storage.get(snapshot_ref, {})
    snapshot_mesh_ref = snapshot.get("spec", {}).get("meshRef")
    if snapshot_mesh_ref != mesh_ref:
        errors.append(create_error(
            "spec.snapshotRef",
            f"snapshot '{snapshot_ref}' belongs to mesh '{snapshot_mesh_ref}', not '{mesh_ref}'",
            "invalid"
        ))

    # Validate scope if present
    scope = spec.get("scope")
    if scope is not None:
        if not isinstance(scope, dict):
            errors.append(create_error("spec.scope", "scope must be an object", "invalid"))
        else:
            valid_keys = {"stores", "blueprints", "tallies", "definitions", "procedures"}
            for key in scope.keys():
                if key not in valid_keys:
                    errors.append(create_error(f"spec.scope.{key}", f"'{key}' is not a valid scope key", "invalid"))

    # Validate resources.memory
    resources = spec.get("resources", {})
    if resources:
        memory = resources.get("memory")
        if memory is not None:
            if not isinstance(memory, dict):
                errors.append(create_error("spec.resources.memory", "memory must be an object", "invalid"))
            else:
                for mem_key in ["limit", "request"]:
                    val = memory.get(mem_key)
                    if val is not None:
                        if not isinstance(val, str):
                            errors.append(create_error(f"spec.resources.memory.{mem_key}", f"{mem_key} must be a string", "invalid"))
                        elif not parse_memory_quantity(val):
                            errors.append(create_error(f"spec.resources.memory.{mem_key}", f"'{val}' is not a valid memory quantity", "invalid"))

        cpu = resources.get("cpu")
        if cpu is not None:
            if not isinstance(cpu, dict):
                errors.append(create_error("spec.resources.cpu", "cpu must be an object", "invalid"))

    # Check for spec immutability on update
    if update and existing:
        existing_spec = existing.get("spec", {})
        if existing_spec != spec:
            errors.append(create_error("spec", "spec is immutable after creation", "immutable"))

    return errors


def create_recovery(data: Dict) -> Dict:
    """Create a new recovery."""
    errors = validate_recovery(data, update=False)
    if errors:
        return {"errors": errors}

    name = data["metadata"]["name"]
    spec = data.get("spec", {})

    # Apply default memory if not specified
    resources = spec.get("resources", {})
    if "memory" not in resources:
        resources = resources.copy()
        resources["memory"] = {"limit": "1Gi", "request": "1Gi"}
        spec = spec.copy()
        spec["resources"] = resources

    recovery = {
        "apiVersion": "mesh.example.com/v1",
        "kind": "Recovery",
        "metadata": data.get("metadata", {}),
        "spec": spec,
        "status": {
            "state": "Initializing"
        }
    }

    storage = load_recoveries_storage()
    storage[name] = recovery
    save_recoveries_storage(storage)

    return recovery


def list_recoveries() -> List[Dict]:
    """List all recovery summaries sorted by name."""
    storage = load_recoveries_storage()
    result = []

    for name in sorted(storage.keys()):
        recovery = storage[name]
        summary = {
            "metadata": {
                "name": name
            },
            "spec": {
                "meshRef": recovery.get("spec", {}).get("meshRef", ""),
                "snapshotRef": recovery.get("spec", {}).get("snapshotRef", "")
            },
            "status": {
                "state": recovery.get("status", {}).get("state", "Unknown")
            }
        }
        result.append(summary)

    return result


def describe_recovery(name: str) -> Dict:
    """Describe a specific recovery."""
    storage = load_recoveries_storage()

    if name not in storage:
        return {"errors": [create_error("metadata.name", f'recovery "{name}" not found', "not_found")]}

    return storage[name]


def update_recovery(data: Dict) -> Dict:
    """Update an existing recovery."""
    recovery_name = data.get("metadata", {}).get("name")

    if not recovery_name:
        return {"errors": [create_error("metadata.name", "metadata.name is required for update", "required")]}

    storage = load_recoveries_storage()
    if recovery_name not in storage:
        return {"errors": [create_error("metadata.name", f'recovery "{recovery_name}" not found', "not_found")]}

    existing = storage[recovery_name]

    errors = validate_recovery(data, update=True, existing=existing)
    if errors:
        return {"errors": errors}

    # Spec is immutable, so no update should happen
    return {"errors": [create_error("spec", "spec is immutable after creation", "immutable")]}


def delete_recovery(name: str) -> Dict:
    """Delete a recovery."""
    storage = load_recoveries_storage()

    if name not in storage:
        return {"errors": [create_error("metadata.name", f'recovery "{name}" not found', "not_found")]}

    del storage[name]
    save_recoveries_storage(storage)

    return {
        "message": f"Recovery '{name}' has been deleted",
        "metadata": {"name": name}
    }


def run_recovery(name: str) -> Dict:
    """Execute a recovery."""
    storage = load_recoveries_storage()

    if name not in storage:
        return {"errors": [create_error("metadata.name", f'recovery "{name}" not found', "not_found")]}

    recovery = storage[name]
    status = recovery.get("status", {})
    current_state = status.get("state", "Unknown")

    # Run is valid only from "Initializing"
    if current_state != "Initializing":
        return {"errors": [create_error(
            "status.state",
            f"resource is in state '{current_state}', expected 'Initializing'",
            "invalid"
        )]}

    # Transition to Running
    recovery["status"]["state"] = "Running"
    storage[name] = recovery
    save_recoveries_storage(storage)

    # Execute the recovery
    spec = recovery.get("spec", {})
    mesh_ref = spec.get("meshRef")

    # Check if mesh is stable
    meshes_storage = load_meshes_storage()
    mesh = meshes_storage.get(mesh_ref, {})
    mesh_stable = mesh.get("status", {}).get("stable", True)

    if not mesh_stable:
        recovery["status"]["state"] = "Unknown"
        recovery["status"]["detail"] = f"mesh '{mesh_ref}' is not stable"
    else:
        recovery["status"]["state"] = "Succeeded"

    storage[name] = recovery
    save_recoveries_storage(storage)

    return recovery


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


def cmd_mesh_shell(name: str):
    """Handle mesh shell command."""
    storage = load_meshes_storage()

    if name not in storage:
        result = {"errors": [create_error("metadata.name", f'mesh "{name}" not found', "not_found")]}
        output_json(result)
        return

    mesh = storage[name]
    spec = mesh.get("spec", {})
    exposure = spec.get("exposure")

    # Check if mesh has exposure configured
    if exposure is None:
        result = {"errors": [create_error("spec.exposure", f"mesh '{name}' has no exposure configured", "invalid")]}
        output_json(result)
        return

    # On success, output connectionDetails only (not the full resource)
    connection_details = compute_connection_details(name, exposure)
    output_json(connection_details)


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


# Task Command Handlers

def cmd_task_create(filepath: str):
    """Handle task create command."""
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

    result = create_task(data)
    output_json(result)


def cmd_task_list():
    """Handle task list command."""
    result = list_tasks()
    output_json(result)


def cmd_task_describe(name: str):
    """Handle task describe command."""
    result = describe_task(name)
    output_json(result)


def cmd_task_delete(name: str):
    """Handle task delete command."""
    result = delete_task(name)
    output_json(result)


def cmd_task_update(filepath: str):
    """Handle task update command."""
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

    result = update_task(data)
    output_json(result)


def cmd_task_run(name: str):
    """Handle task run command."""
    result = run_task(name)
    output_json(result)


# Snapshot Command Handlers

def cmd_snapshot_create(filepath: str):
    """Handle snapshot create command."""
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

    result = create_snapshot(data)
    output_json(result)


def cmd_snapshot_list():
    """Handle snapshot list command."""
    result = list_snapshots()
    output_json(result)


def cmd_snapshot_describe(name: str):
    """Handle snapshot describe command."""
    result = describe_snapshot(name)
    output_json(result)


def cmd_snapshot_delete(name: str):
    """Handle snapshot delete command."""
    result = delete_snapshot(name)
    output_json(result)


def cmd_snapshot_update(filepath: str):
    """Handle snapshot update command."""
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

    result = update_snapshot(data)
    output_json(result)


def cmd_snapshot_run(name: str):
    """Handle snapshot run command."""
    result = run_snapshot(name)
    output_json(result)


# Recovery Command Handlers

def cmd_recovery_create(filepath: str):
    """Handle recovery create command."""
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

    result = create_recovery(data)
    output_json(result)


def cmd_recovery_list():
    """Handle recovery list command."""
    result = list_recoveries()
    output_json(result)


def cmd_recovery_describe(name: str):
    """Handle recovery describe command."""
    result = describe_recovery(name)
    output_json(result)


def cmd_recovery_delete(name: str):
    """Handle recovery delete command."""
    result = delete_recovery(name)
    output_json(result)


def cmd_recovery_update(filepath: str):
    """Handle recovery update command."""
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

    result = update_recovery(data)
    output_json(result)


def cmd_recovery_run(name: str):
    """Handle recovery run command."""
    result = run_recovery(name)
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

    # mesh shell
    shell_parser = mesh_subparsers.add_parser("shell", help="Open a shell to the mesh")
    shell_parser.add_argument("name", help="Mesh name")

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

    # task subcommand
    task_parser = subparsers.add_parser("task", help="Task resource operations")
    task_subparsers = task_parser.add_subparsers(dest="operation", help="Task operations")

    # task create
    task_create_parser = task_subparsers.add_parser("create", help="Create a task from YAML")
    task_create_parser.add_argument("-f", "--file", required=True, help="YAML file path")

    # task list
    task_subparsers.add_parser("list", help="List task summaries")

    # task describe
    task_describe_parser = task_subparsers.add_parser("describe", help="Print the full task")
    task_describe_parser.add_argument("name", help="Task name")

    # task delete
    task_delete_parser = task_subparsers.add_parser("delete", help="Delete the task")
    task_delete_parser.add_argument("name", help="Task name")

    # task update
    task_update_parser = task_subparsers.add_parser("update", help="Apply a partial update")
    task_update_parser.add_argument("-f", "--file", required=True, help="YAML file path")

    # task run
    task_run_parser = task_subparsers.add_parser("run", help="Execute the task")
    task_run_parser.add_argument("name", help="Task name")

    # snapshot subcommand
    snapshot_parser = subparsers.add_parser("snapshot", help="Snapshot resource operations")
    snapshot_subparsers = snapshot_parser.add_subparsers(dest="operation", help="Snapshot operations")

    # snapshot create
    snapshot_create_parser = snapshot_subparsers.add_parser("create", help="Create a snapshot from YAML")
    snapshot_create_parser.add_argument("-f", "--file", required=True, help="YAML file path")

    # snapshot list
    snapshot_subparsers.add_parser("list", help="List snapshot summaries")

    # snapshot describe
    snapshot_describe_parser = snapshot_subparsers.add_parser("describe", help="Print the full snapshot")
    snapshot_describe_parser.add_argument("name", help="Snapshot name")

    # snapshot delete
    snapshot_delete_parser = snapshot_subparsers.add_parser("delete", help="Delete the snapshot")
    snapshot_delete_parser.add_argument("name", help="Snapshot name")

    # snapshot update
    snapshot_update_parser = snapshot_subparsers.add_parser("update", help="Apply a partial update")
    snapshot_update_parser.add_argument("-f", "--file", required=True, help="YAML file path")

    # snapshot run
    snapshot_run_parser = snapshot_subparsers.add_parser("run", help="Execute the snapshot")
    snapshot_run_parser.add_argument("name", help="Snapshot name")

    # recovery subcommand
    recovery_parser = subparsers.add_parser("recovery", help="Recovery resource operations")
    recovery_subparsers = recovery_parser.add_subparsers(dest="operation", help="Recovery operations")

    # recovery create
    recovery_create_parser = recovery_subparsers.add_parser("create", help="Create a recovery from YAML")
    recovery_create_parser.add_argument("-f", "--file", required=True, help="YAML file path")

    # recovery list
    recovery_subparsers.add_parser("list", help="List recovery summaries")

    # recovery describe
    recovery_describe_parser = recovery_subparsers.add_parser("describe", help="Print the full recovery")
    recovery_describe_parser.add_argument("name", help="Recovery name")

    # recovery delete
    recovery_delete_parser = recovery_subparsers.add_parser("delete", help="Delete the recovery")
    recovery_delete_parser.add_argument("name", help="Recovery name")

    # recovery update
    recovery_update_parser = recovery_subparsers.add_parser("update", help="Apply a partial update")
    recovery_update_parser.add_argument("-f", "--file", required=True, help="YAML file path")

    # recovery run
    recovery_run_parser = recovery_subparsers.add_parser("run", help="Execute the recovery")
    recovery_run_parser.add_argument("name", help="Recovery name")

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
        elif args.operation == "shell":
            cmd_mesh_shell(args.name)
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
    elif args.command == "task":
        if args.operation == "create":
            cmd_task_create(args.file)
        elif args.operation == "list":
            cmd_task_list()
        elif args.operation == "describe":
            cmd_task_describe(args.name)
        elif args.operation == "delete":
            cmd_task_delete(args.name)
        elif args.operation == "update":
            cmd_task_update(args.file)
        elif args.operation == "run":
            cmd_task_run(args.name)
        else:
            parser.print_help()
    elif args.command == "snapshot":
        if args.operation == "create":
            cmd_snapshot_create(args.file)
        elif args.operation == "list":
            cmd_snapshot_list()
        elif args.operation == "describe":
            cmd_snapshot_describe(args.name)
        elif args.operation == "delete":
            cmd_snapshot_delete(args.name)
        elif args.operation == "update":
            cmd_snapshot_update(args.file)
        elif args.operation == "run":
            cmd_snapshot_run(args.name)
        else:
            parser.print_help()
    elif args.command == "recovery":
        if args.operation == "create":
            cmd_recovery_create(args.file)
        elif args.operation == "list":
            cmd_recovery_list()
        elif args.operation == "describe":
            cmd_recovery_describe(args.name)
        elif args.operation == "delete":
            cmd_recovery_delete(args.name)
        elif args.operation == "update":
            cmd_recovery_update(args.file)
        elif args.operation == "run":
            cmd_recovery_run(args.name)
        else:
            parser.print_help()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
