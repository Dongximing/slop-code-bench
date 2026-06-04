#!/usr/bin/env python3
"""
meshctl: Manage mesh resources from YAML specs
"""

import argparse
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

import yaml


# Persistent storage for meshes (in-memory for this implementation)
MESHES_STORE: Dict[str, Dict[str, Any]] = {}
STORAGE_FILE = "meshes.json"


def load_storage() -> None:
    """Load persisted meshes from disk."""
    global MESHES_STORE
    if os.path.exists(STORAGE_FILE):
        try:
            with open(STORAGE_FILE, 'r') as f:
                MESHES_STORE = json.load(f)
        except (json.JSONDecodeError, IOError):
            MESHES_STORE = {}


def save_storage() -> None:
    """Persist meshes to disk."""
    with open(STORAGE_FILE, 'w') as f:
        json.dump(MESHES_STORE, f, indent=2)


class Error:
    """Error object for validation and operation errors."""
    def __init__(self, field: str, message: str, error_type: str):
        self.field = field
        self.message = message
        self.type = error_type

    def to_dict(self) -> Dict[str, str]:
        return {
            "field": self.field,
            "message": self.message,
            "type": self.type
        }


def output_errors(errors: List[Error]) -> None:
    """Output errors as JSON to stdout."""
    result = {"errors": [e.to_dict() for e in errors]}
    print(json.dumps(result))
    sys.exit(1)


def parse_memory_quantity(value: str) -> int:
    """
    Parse memory quantity string to bytes.
    Accepted forms: non-negative integer (bytes), or with Ki, Mi, Gi, Ti suffix.
    Returns bytes as integer.
    """
    if not isinstance(value, str):
        raise ValueError("Memory quantity must be a string")

    value = value.strip()
    if not value:
        raise ValueError("Memory quantity cannot be empty")

    # Check for suffix
    suffix_multipliers = {
        'Ki': 1024,
        'Mi': 1024 * 1024,
        'Gi': 1024 * 1024 * 1024,
        'Ti': 1024 * 1024 * 1024 * 1024,
    }

    for suffix, multiplier in suffix_multipliers.items():
        if value.endswith(suffix):
            num_str = value[:-len(suffix)]
            if not num_str.isdigit() and not (num_str.startswith('0') and num_str != '0'):
                try:
                    num = int(num_str)
                except ValueError:
                    raise ValueError(f"Invalid memory format: {value}")
            else:
                try:
                    num = int(num_str)
                except ValueError:
                    raise ValueError(f"Invalid memory format: {value}")
            if num < 0:
                raise ValueError(f"Memory must be non-negative: {value}")
            return num * multiplier

    # Plain integer (bytes)
    if not value.isdigit() and not (value.startswith('0') and value != '0'):
        try:
            int(value)
        except ValueError:
            raise ValueError(f"Invalid memory format: {value}")
    try:
        num = int(value)
    except ValueError:
        raise ValueError(f"Invalid memory format: {value}")
    if num < 0:
        raise ValueError(f"Memory must be non-negative: {value}")
    return num


def parse_cpu_quantity(value: str) -> int:
    """
    Parse CPU quantity string to milli-cores.
    Accepted forms: non-negative integer core count, or with 'm' suffix.
    Returns milli-cores as integer.
    """
    if not isinstance(value, str):
        raise ValueError("CPU quantity must be a string")

    value = value.strip()
    if not value:
        raise ValueError("CPU quantity cannot be empty")

    if value.endswith('m'):
        num_str = value[:-1]
        if not num_str.isdigit() and not (num_str.startswith('0') and num_str != '0'):
            try:
                int(num_str)
            except ValueError:
                raise ValueError(f"Invalid CPU format: {value}")
        try:
            num = int(num_str)
        except ValueError:
            raise ValueError(f"Invalid CPU format: {value}")
        if num < 0:
            raise ValueError(f"CPU must be non-negative: {value}")
        return num
    else:
        # Plain integer (cores)
        if not value.isdigit() and not (value.startswith('0') and value != '0'):
            try:
                int(value)
            except ValueError:
                raise ValueError(f"Invalid CPU format: {value}")
        try:
            num = int(value)
        except ValueError:
            raise ValueError(f"Invalid CPU format: {value}")
        if num < 0:
            raise ValueError(f"CPU must be non-negative: {value}")
        return num * 1000


def validate_name(name: Any) -> List[Error]:
    """Validate metadata.name field."""
    errors = []

    if name is None:
        errors.append(Error("metadata.name", "name is required", "required"))
        return errors

    if not isinstance(name, str):
        errors.append(Error("metadata.name", "name must be a string", "invalid"))
        return errors

    if name == "":
        errors.append(Error("metadata.name", "name cannot be empty", "required"))
        return errors

    # Must match ^[a-z0-9][a-z0-9-]*[a-z0-9]$
    # Minimum length 2
    if len(name) < 2:
        errors.append(Error("metadata.name", "name must be at least 2 characters", "invalid"))
        return errors

    pattern = r'^[a-z0-9][a-z0-9-]*[a-z0-9]$'
    if not re.match(pattern, name):
        errors.append(Error("metadata.name", "name must match ^[a-z0-9][a-z0-9-]*[a-z0-9]$", "invalid"))

    return errors


def validate_runtime(runtime: Any) -> List[Error]:
    """Validate spec.runtime field."""
    errors = []

    if runtime is None:
        return errors

    if not isinstance(runtime, str):
        errors.append(Error("spec.runtime", "runtime must be a string", "invalid"))
        return errors

    # Must be semantic version major.minor.patch
    pattern = r'^\d+\.\d+\.\d+$'
    if not re.match(pattern, runtime):
        errors.append(Error("spec.runtime", "runtime must be semantic version major.minor.patch", "invalid"))

    return errors


def validate_instances(instances: Any) -> List[Error]:
    """Validate spec.instances field."""
    errors = []

    if instances is None:
        return errors

    if not isinstance(instances, int):
        errors.append(Error("spec.instances", "instances must be an integer", "invalid"))
        return errors

    if instances <= 0:
        errors.append(Error("spec.instances", "instances must be positive", "invalid"))

    return errors


def validate_resources_memory(memory: Any) -> List[Error]:
    """Validate spec.resources.memory field."""
    errors = []

    if memory is None:
        return errors

    if not isinstance(memory, dict):
        errors.append(Error("spec.resources.memory", "resources.memory must be an object", "invalid"))
        return errors

    # limit is required when memory is present
    if 'limit' not in memory:
        errors.append(Error("spec.resources.memory.limit", "memory limit is required", "required"))
        return errors

    # Validate limit format
    try:
        limit_bytes = parse_memory_quantity(memory['limit'])
    except ValueError as e:
        errors.append(Error("spec.resources.memory.limit", str(e), "invalid"))
        return errors  # Can't validate request if limit is invalid

    # Validate request if present
    if 'request' in memory:
        request_val = memory['request']
        if request_val is not None:
            try:
                request_bytes = parse_memory_quantity(request_val)
                if request_bytes > limit_bytes:
                    errors.append(Error("spec.resources.memory.request",
                                       "request cannot exceed limit", "invalid"))
            except ValueError as e:
                errors.append(Error("spec.resources.memory.request", str(e), "invalid"))

    return errors


def validate_resources_cpu(cpu: Any) -> List[Error]:
    """Validate spec.resources.cpu field."""
    errors = []

    if cpu is None:
        return errors

    if not isinstance(cpu, dict):
        errors.append(Error("spec.resources.cpu", "resources.cpu must be an object", "invalid"))
        return errors

    # limit is required when cpu is present
    if 'limit' not in cpu:
        errors.append(Error("spec.resources.cpu.limit", "cpu limit is required", "required"))
        return errors

    # Validate limit format
    try:
        limit_milli = parse_cpu_quantity(cpu['limit'])
    except ValueError as e:
        errors.append(Error("spec.resources.cpu.limit", str(e), "invalid"))
        return errors  # Can't validate request if limit is invalid

    # Validate request if present
    if 'request' in cpu:
        request_val = cpu['request']
        if request_val is not None:
            try:
                request_milli = parse_cpu_quantity(request_val)
                if request_milli > limit_milli:
                    errors.append(Error("spec.resources.cpu.request",
                                       "request cannot exceed limit", "invalid"))
            except ValueError as e:
                errors.append(Error("spec.resources.cpu.request", str(e), "invalid"))

    return errors


def validate_migration_strategy(strategy: Any) -> List[Error]:
    """Validate spec.migration.strategy field."""
    errors = []

    if strategy is None:
        return errors

    if not isinstance(strategy, str):
        errors.append(Error("spec.migration.strategy", "migration strategy must be a string", "invalid"))
        return errors

    if strategy != "FullStop":
        errors.append(Error("spec.migration.strategy", "migration strategy must be 'FullStop'", "invalid"))

    return errors


def find_auto_scaling(obj: Any, path: str = "spec") -> List[Error]:
    """Recursively find autoScaling fields under spec."""
    errors = []

    if not isinstance(obj, dict):
        return errors

    if 'autoScaling' in obj:
        errors.append(Error(path, "autoScaling is forbidden", "forbidden"))
        # Don't recurse into autoScaling
        return errors

    for key, value in obj.items():
        new_path = f"{path}.{key}" if path else key
        if isinstance(value, dict):
            errors.extend(find_auto_scaling(value, new_path))

    return errors


def validate_yaml_data(data: Any, is_create: bool = True) -> List[Error]:
    """Validate the parsed YAML data."""
    errors = []

    # Must be a mapping
    if not isinstance(data, dict):
        errors.append(Error("", "YAML document must be a mapping", "parse"))
        return errors

    # Check for top-level keys
    if 'metadata' not in data:
        errors.append(Error("metadata", "metadata is required", "required"))
        return errors

    if 'spec' not in data:
        errors.append(Error("spec", "spec is required", "required"))
        return errors

    metadata = data.get('metadata')
    spec = data.get('spec')

    # Validate metadata.name
    if metadata is not None:
        errors.extend(validate_name(metadata.get('name')))

    # Validate spec fields
    if spec is not None:
        # Validate instances
        errors.extend(validate_instances(spec.get('instances')))

        # Validate runtime
        errors.extend(validate_runtime(spec.get('runtime')))

        # Validate resources
        if 'resources' in spec:
            resources = spec['resources']
            if isinstance(resources, dict):
                errors.extend(validate_resources_memory(resources.get('memory')))
                errors.extend(validate_resources_cpu(resources.get('cpu')))

        # Validate migration.strategy
        if 'migration' in spec:
            migration = spec['migration']
            if isinstance(migration, dict):
                errors.extend(validate_migration_strategy(migration.get('strategy')))

        # Check for forbidden autoScaling fields
        errors.extend(find_auto_scaling(spec))

    # Check for duplicate on create
    if is_create and metadata is not None:
        name = metadata.get('name')
        if name and name in MESHES_STORE:
            errors.append(Error("metadata.name", f"mesh '{name}' already exists", "duplicate"))

    # For describe/delete, check if mesh exists
    # (handled by caller)

    return errors


def apply_defaults(data: Dict[str, Any]) -> Dict[str, Any]:
    """Apply default values to the data."""
    result = {
        "metadata": {},
        "spec": {},
        "status": {"state": "Running"}
    }

    # Copy metadata
    if 'metadata' in data and isinstance(data['metadata'], dict):
        result['metadata'] = dict(data['metadata'])

    # Copy spec and apply defaults
    spec = data.get('spec', {})
    if not isinstance(spec, dict):
        spec = {}

    result['spec'] = {}

    # instances: default 1
    result['spec']['instances'] = spec.get('instances', 1)

    # runtime: omit when absent (no default)
    if 'runtime' in spec:
        result['spec']['runtime'] = spec['runtime']

    # resources
    resources = spec.get('resources', {})
    if isinstance(resources, dict):
        result['spec']['resources'] = {}

        # memory: Omit the whole object to get {"limit": "1Gi", "request": "1Gi"}
        memory = resources.get('memory')
        if memory is None:
            # Memory object was completely omitted, add with defaults
            result['spec']['resources']['memory'] = {"limit": "1Gi", "request": "1Gi"}
        elif isinstance(memory, dict):
            result['spec']['resources']['memory'] = {}
            if 'limit' in memory:
                result['spec']['resources']['memory']['limit'] = memory['limit']
            # request defaults to limit
            if 'request' in memory:
                result['spec']['resources']['memory']['request'] = memory['request']
            else:
                # If limit is present, request defaults to limit
                if 'limit' in memory:
                    result['spec']['resources']['memory']['request'] = memory['limit']

        # cpu: omit when absent
        cpu = resources.get('cpu')
        if cpu is not None and isinstance(cpu, dict):
            result['spec']['resources']['cpu'] = {}
            if 'limit' in cpu:
                result['spec']['resources']['cpu']['limit'] = cpu['limit']
            if 'request' in cpu:
                result['spec']['resources']['cpu']['request'] = cpu['request']
            # request defaults to limit when present but no request
            elif 'limit' in cpu:
                result['spec']['resources']['cpu']['request'] = cpu['limit']

    # access.authentication.enabled: default true
    access = spec.get('access', {})
    if isinstance(access, dict):
        authentication = access.get('authentication')
        if authentication is not None and isinstance(authentication, dict):
            result['spec']['access'] = {'authentication': {}}
            if 'enabled' in authentication:
                result['spec']['access']['authentication']['enabled'] = authentication['enabled']
            else:
                result['spec']['access']['authentication']['enabled'] = True
        else:
            # Default to true when access exists but no authentication
            result['spec']['access'] = {'authentication': {'enabled': True}}
    else:
        # Default: authentication enabled
        result['spec']['access'] = {'authentication': {'enabled': True}}

    # migration.strategy: default "FullStop"
    migration = spec.get('migration', {})
    if isinstance(migration, dict):
        result['spec']['migration'] = {'strategy': migration.get('strategy', 'FullStop')}
    else:
        result['spec']['migration'] = {'strategy': 'FullStop'}

    return result


def read_yaml_file(path: str) -> Tuple[Optional[Dict[str, Any]], List[Error]]:
    """Read and parse a YAML file."""
    errors = []

    try:
        with open(path, 'r') as f:
            content = f.read()
    except IOError as e:
        errors.append(Error("", f"Cannot read file: {e}", "parse"))
        return None, errors

    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as e:
        errors.append(Error("", f"Cannot parse YAML: {e}", "parse"))
        return None, errors

    return data, errors


def cmd_create(file_path: str) -> None:
    """Handle mesh create command."""
    load_storage()

    data, errors = read_yaml_file(file_path)
    if errors:
        output_errors(errors)

    # Validate
    errors = validate_yaml_data(data, is_create=True)
    if errors:
        output_errors(errors)

    # Apply defaults
    full_resource = apply_defaults(data)

    # Persist
    name = full_resource['metadata']['name']
    MESHES_STORE[name] = full_resource
    save_storage()

    # Output full resource
    print(json.dumps(full_resource))


def cmd_list() -> None:
    """Handle mesh list command."""
    load_storage()

    # Create list of summaries sorted by name ascending, case-sensitive
    meshes = []
    for name in sorted(MESHES_STORE.keys()):
        mesh = MESHES_STORE[name]
        meshes.append({
            "name": name,
            "status": mesh.get('status', {"state": "Unknown"})
        })

    print(json.dumps(meshes))


def cmd_describe(name: str) -> None:
    """Handle mesh describe command."""
    load_storage()

    # Check if mesh exists
    if name not in MESHES_STORE:
        errors = [Error("metadata.name", f"mesh '{name}' not found", "not_found")]
        output_errors(errors)

    print(json.dumps(MESHES_STORE[name]))


def cmd_delete(name: str) -> None:
    """Handle mesh delete command."""
    load_storage()

    # Check if mesh exists
    if name not in MESHES_STORE:
        errors = [Error("metadata.name", f"mesh '{name}' not found", "not_found")]
        output_errors(errors)

    # Delete
    del MESHES_STORE[name]
    save_storage()

    # Output confirmation
    result = {
        "message": f"Mesh '{name}' deleted successfully",
        "metadata": {"name": name}
    }
    print(json.dumps(result))


def main() -> None:
    parser = argparse.ArgumentParser(
        prog='meshctl',
        description='Manage mesh resources from YAML specs'
    )
    subparsers = parser.add_subparsers(dest='command', required=True)

    # mesh subcommand
    mesh_parser = subparsers.add_parser('mesh', help='Manage mesh resources')
    mesh_subparsers = mesh_parser.add_subparsers(dest='operation', required=True)

    # mesh create
    create_parser = mesh_subparsers.add_parser('create', help='Create a mesh from YAML')
    create_parser.add_argument('-f', '--file', required=True, help='YAML file path')

    # mesh list
    mesh_subparsers.add_parser('list', help='List all meshes')

    # mesh describe
    describe_parser = mesh_subparsers.add_parser('describe', help='Describe a mesh')
    describe_parser.add_argument('name', help='Mesh name')

    # mesh delete
    delete_parser = mesh_subparsers.add_parser('delete', help='Delete a mesh')
    delete_parser.add_argument('name', help='Mesh name')

    args = parser.parse_args()

    if args.command == 'mesh':
        if args.operation == 'create':
            cmd_create(args.file)
        elif args.operation == 'list':
            cmd_list()
        elif args.operation == 'describe':
            cmd_describe(args.name)
        elif args.operation == 'delete':
            cmd_delete(args.name)


if __name__ == '__main__':
    main()
