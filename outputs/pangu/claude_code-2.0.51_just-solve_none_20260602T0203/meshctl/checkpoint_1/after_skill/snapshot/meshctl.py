#!/usr/bin/env python3
"""
meshctl - Manages mesh resources from YAML specs and prints JSON to stdout.
"""

import argparse
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import yaml


# Storage file for meshes
STORAGE_FILE = 'meshes.storage'


def load_meshes() -> Dict[str, Dict[str, Any]]:
    """Load meshes from storage file."""
    if os.path.exists(STORAGE_FILE):
        try:
            with open(STORAGE_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_meshes(meshes: Dict[str, Dict[str, Any]]) -> None:
    """Save meshes to storage file."""
    with open(STORAGE_FILE, 'w') as f:
        json.dump(meshes, f)


# Storage for meshes (file-based)
MEShes = load_meshes()


def parse_memory_quantity(value: str) -> Tuple[int, str]:
    """
    Parse a memory quantity string.
    Returns (bytes, unit) where unit is one of: 'B', 'Ki', 'Mi', 'Gi', 'Ti'.
    Accepts: non-negative integer (bytes), or integer with Ki, Mi, Gi, Ti suffix.
    """
    if value is None:
        raise ValueError("Memory quantity cannot be null")

    match = re.match(r'^(\d+)(Ki|Mi|Gi|Ti)?$', value)
    if not match:
        raise ValueError(f"Invalid memory quantity: {value}")

    num = int(match.group(1))
    unit = match.group(2) or 'B'

    if num < 0:
        raise ValueError(f"Memory quantity must be non-negative: {value}")

    return num, unit


def parse_cpu_quantity(value: str) -> Tuple[int, str]:
    """
    Parse a CPU quantity string.
    Returns (cores, unit) where unit is one of: 'cores', 'm' (millicores).
    Accepts: non-negative integer, or integer with 'm' suffix.
    """
    if value is None:
        raise ValueError("CPU quantity cannot be null")

    match = re.match(r'^(\d+)(m)?$', value)
    if not match:
        raise ValueError(f"Invalid CPU quantity: {value}")

    num = int(match.group(1))
    unit = match.group(2) or 'cores'

    if num < 0:
        raise ValueError(f"CPU quantity must be non-negative: {value}")

    return num, unit


def normalize_memory_quantity(value: str) -> str:
    num, unit = parse_memory_quantity(value)
    if unit == 'B':
        return value
    return f"{num}{unit}"


def normalize_cpu_quantity(value: str) -> str:
    num, unit = parse_cpu_quantity(value)
    if unit == 'cores':
        return str(num)
    return f"{num}{unit}"


def memory_to_bytes(quantity: str) -> int:
    """Convert memory quantity to absolute bytes for comparison."""
    num, unit = parse_memory_quantity(quantity)

    multipliers = {
        'B': 1,
        'Ki': 1024,
        'Mi': 1024 * 1024,
        'Gi': 1024 * 1024 * 1024,
        'Ti': 1024 * 1024 * 1024 * 1024,
    }

    return num * multipliers[unit]


def cpu_to_millicores(quantity: str) -> int:
    """Convert CPU quantity to absolute millicores for comparison."""
    num, unit = parse_cpu_quantity(quantity)

    if unit == 'cores':
        return num * 1000
    return num


def validate_name(name: str) -> List[Dict[str, str]]:
    """Validate metadata.name field. Returns list of errors."""
    errors = []

    if name is None or name == '':
        errors.append({
            "field": "metadata.name",
            "message": "metadata.name is required",
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

    # Check pattern: ^[a-z0-9][a-z0-9-]*[a-z0-9]$
    # Minimum length 2
    if len(name) < 2:
        errors.append({
            "field": "metadata.name",
            "message": "metadata.name must be at least 2 characters",
            "type": "invalid"
        })
        return errors

    if not re.match(r'^[a-z0-9][a-z0-9-]*[a-z0-9]$', name):
        errors.append({
            "field": "metadata.name",
            "message": "metadata.name must match pattern ^[a-z0-9][a-z0-9-]*[a-z0-9]$",
            "type": "invalid"
        })

    return errors


def validate_runtime(runtime: str) -> List[Dict[str, str]]:
    """Validate spec.runtime field. Returns list of errors."""
    if runtime is None:
        return []

    errors = []

    if not isinstance(runtime, str):
        errors.append({
            "field": "spec.runtime",
            "message": "spec.runtime must be a string",
            "type": "invalid"
        })
        return errors

    parts = runtime.split('.')
    if len(parts) != 3:
        errors.append({
            "field": "spec.runtime",
            "message": f"spec.runtime must be in major.minor.patch format, got {runtime}",
            "type": "invalid"
        })
        return errors

    for part in parts:
        if not part.isdigit():
            errors.append({
                "field": "spec.runtime",
                "message": f"spec.runtime parts must be non-negative integers, got {runtime}",
                "type": "invalid"
            })
            return errors

    return errors


def validate_instances(instances: Any) -> List[Dict[str, str]]:
    """Validate spec.instances field. Returns list of errors."""
    if instances is None:
        return []

    errors = []

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


def validate_resources(spec: Dict[str, Any]) -> List[Dict[str, str]]:
    """Validate spec.resources field. Returns list of errors."""
    errors = []
    resources = spec.get('resources')

    if resources is None:
        return errors

    if not isinstance(resources, dict):
        errors.append({
            "field": "spec.resources",
            "message": "spec.resources must be a mapping",
            "type": "invalid"
        })
        return errors

    # Validate memory resources
    memory = resources.get('memory')
    if memory is not None:
        if not isinstance(memory, dict):
            errors.append({
                "field": "spec.resources.memory",
                "message": "spec.resources.memory must be a mapping",
                "type": "invalid"
            })
        else:
            memory_limit = memory.get('limit')
            memory_request = memory.get('request')

            # Limit is required when memory is present
            if memory_limit is None:
                errors.append({
                    "field": "spec.resources.memory.limit",
                    "message": "spec.resources.memory.limit is required",
                    "type": "required"
                })
            else:
                try:
                    normalize_memory_quantity(str(memory_limit))
                except ValueError as e:
                    errors.append({
                        "field": "spec.resources.memory.limit",
                        "message": str(e),
                        "type": "invalid"
                    })

                if memory_request is not None:
                    try:
                        limit_bytes = memory_to_bytes(str(memory_limit))
                        request_bytes = memory_to_bytes(str(memory_request))
                        if request_bytes > limit_bytes:
                            errors.append({
                                "field": "spec.resources.memory.request",
                                "message": "spec.resources.memory.request must not exceed limit",
                                "type": "invalid"
                            })
                    except ValueError as e:
                        errors.append({
                            "field": "spec.resources.memory.request",
                            "message": str(e),
                            "type": "invalid"
                        })
                else:
                    # Request defaults to limit
                    pass

    # Validate CPU resources
    cpu = resources.get('cpu')
    if cpu is not None:
        if not isinstance(cpu, dict):
            errors.append({
                "field": "spec.resources.cpu",
                "message": "spec.resources.cpu must be a mapping",
                "type": "invalid"
            })
        else:
            cpu_limit = cpu.get('limit')
            cpu_request = cpu.get('request')

            # Limit is required when cpu is present
            if cpu_limit is None:
                errors.append({
                    "field": "spec.resources.cpu.limit",
                    "message": "spec.resources.cpu.limit is required",
                    "type": "required"
                })
            else:
                try:
                    normalize_cpu_quantity(str(cpu_limit))
                except ValueError as e:
                    errors.append({
                        "field": "spec.resources.cpu.limit",
                        "message": str(e),
                        "type": "invalid"
                    })

                if cpu_request is not None:
                    try:
                        limit_millicores = cpu_to_millicores(str(cpu_limit))
                        request_millicores = cpu_to_millicores(str(cpu_request))
                        if request_millicores > limit_millicores:
                            errors.append({
                                "field": "spec.resources.cpu.request",
                                "message": "spec.resources.cpu.request must not exceed limit",
                                "type": "invalid"
                            })
                    except ValueError as e:
                        errors.append({
                            "field": "spec.resources.cpu.request",
                            "message": str(e),
                            "type": "invalid"
                        })
                else:
                    # Request defaults to limit
                    pass

    return errors


def validate_authentication(access: Dict[str, Any]) -> List[Dict[str, str]]:
    """Validate spec.access.authentication field. Returns list of errors."""
    errors = []
    authentication = access.get('authentication')

    if authentication is not None:
        if not isinstance(authentication, dict):
            errors.append({
                "field": "spec.access.authentication",
                "message": "spec.access.authentication must be a mapping",
                "type": "invalid"
            })
        else:
            enabled = authentication.get('enabled')
            if enabled is not None and not isinstance(enabled, bool):
                errors.append({
                    "field": "spec.access.authentication.enabled",
                    "message": "spec.access.authentication.enabled must be a boolean",
                    "type": "invalid"
                })

    return errors


def validate_migration_strategy(strategy: Any) -> List[Dict[str, str]]:
    """Validate spec.migration.strategy field. Returns list of errors."""
    if strategy is None:
        return []

    errors = []

    if not isinstance(strategy, str):
        errors.append({
            "field": "spec.migration.strategy",
            "message": "spec.migration.strategy must be a string",
            "type": "invalid"
        })
        return errors

    if strategy != "FullStop":
        errors.append({
            "field": "spec.migration.strategy",
            "message": "spec.migration.strategy must be 'FullStop'",
            "type": "invalid"
        })

    return errors


def find_nested_fields(d: Dict[str, Any], parent_key: str = "") -> List[str]:
    """Find all nested field names under spec, specifically looking for autoScaling."""
    errors = []

    if not isinstance(d, dict):
        return errors

    for key, value in d.items():
        full_key = f"{parent_key}.{key}" if parent_key else key

        if key == 'autoScaling':
            errors.append(full_key)

        if isinstance(value, dict):
            errors.extend(find_nested_fields(value, full_key))

    return errors


def validate_no_autoscaling(spec: Dict[str, Any]) -> List[Dict[str, str]]:
    """Validate that no autoScaling field exists under spec. Returns list of errors."""
    errors = []

    auto_scaling_fields = find_nested_fields(spec, "spec")

    for field in auto_scaling_fields:
        errors.append({
            "field": field,
            "message": f"{field} is forbidden",
            "type": "forbidden"
        })

    return errors


def validate_yaml(data: Dict[str, Any]) -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
    """
    Validate the YAML data and return any errors along with defaulted spec.
    """
    errors = []

    # Basic structure check
    if not isinstance(data, dict):
        errors.append({
            "field": "",
            "message": "Input must be a mapping",
            "type": "parse"
        })
        return errors, {}

    # Validate metadata.name
    metadata = data.get('metadata')
    if metadata is None:
        errors.append({
            "field": "metadata.name",
            "message": "metadata.name is required",
            "type": "required"
        })
    elif not isinstance(metadata, dict):
        errors.append({
            "field": "metadata.name",
            "message": "metadata.name is required",
            "type": "required"
        })
    else:
        errors.extend(validate_name(metadata.get('name')))

    # Validate spec
    spec = data.get('spec')
    if spec is None:
        spec = {}

    if not isinstance(spec, dict):
        errors.append({
            "field": "spec",
            "message": "spec must be a mapping",
            "type": "invalid"
        })
        return errors, {}

    # Validate spec fields
    errors.extend(validate_runtime(spec.get('runtime')))
    errors.extend(validate_instances(spec.get('instances')))
    errors.extend(validate_resources(spec))
    errors.extend(validate_no_autoscaling(spec))

    # Validate access
    access = spec.get('access')
    if access is not None:
        if not isinstance(access, dict):
            errors.append({
                "field": "spec.access",
                "message": "spec.access must be a mapping",
                "type": "invalid"
            })
        else:
            errors.extend(validate_authentication(access))

    # Validate migration
    migration = spec.get('migration')
    if migration is not None:
        if not isinstance(migration, dict):
            errors.append({
                "field": "spec.migration",
                "message": "spec.migration must be a mapping",
                "type": "invalid"
            })
        else:
            errors.extend(validate_migration_strategy(migration.get('strategy')))
    else:
        # Default migration.strategy to "FullStop"
        migration = {}
        migration['strategy'] = "FullStop"
        spec['migration'] = migration

    return errors, spec


def apply_defaults(spec: Dict[str, Any]) -> Dict[str, Any]:
    defaulted = spec.copy()

    # Default instances to 1
    if 'instances' not in defaulted:
        defaulted['instances'] = 1

    # Default access.authentication.enabled to true
    if 'access' not in defaulted:
        defaulted['access'] = {}

    access = defaulted['access']
    if 'authentication' not in access:
        access['authentication'] = {}

    auth = access['authentication']
    if 'enabled' not in auth:
        auth['enabled'] = True

    # Default migration.strategy to "FullStop" (already set in validation)
    if 'migration' not in defaulted:
        defaulted['migration'] = {}
        defaulted['migration']['strategy'] = "FullStop"
    elif 'strategy' not in defaulted['migration']:
        defaulted['migration']['strategy'] = "FullStop"

    # Default resources.memory when absent
    if 'resources' not in defaulted:
        defaulted['resources'] = {
            "memory": {
                "limit": "1Gi",
                "request": "1Gi"
            }
        }
    else:
        resources = defaulted['resources']

        # Handle memory defaults
        if 'memory' not in resources or resources['memory'] is None:
            resources['memory'] = {"limit": "1Gi", "request": "1Gi"}

        if isinstance(resources.get('memory'), dict):
            memory = resources['memory']
            memory_limit = memory.get('limit')

            if memory_limit is not None and 'request' not in memory:
                # Request defaults to limit
                memory['request'] = memory_limit

        # Handle CPU defaults if cpu is present
        if isinstance(resources.get('cpu'), dict):
            cpu = resources['cpu']
            cpu_limit = cpu.get('limit')

            if cpu_limit is not None and 'request' not in cpu:
                # Request defaults to limit
                cpu['request'] = cpu_limit

    return defaulted


def create_resource(data: Dict[str, Any]) -> Tuple[bool, Dict[str, Any], List[Dict[str, str]]]:
    """
    Create a new mesh resource from YAML data.
    Returns (success, resource, errors).
    """
    validation_errors, spec = validate_yaml(data)

    if validation_errors:
        return False, {}, validation_errors

    name = data['metadata']['name']

    # Check for duplicate
    if name in MEShes:
        return False, {}, [{
            "field": "metadata.name",
            "message": f"mesh '{name}' already exists",
            "type": "duplicate"
        }]

    # Apply defaults
    spec = apply_defaults(spec)

    # Build the full resource
    resource = {
        "metadata": {
            "name": name
        },
        "spec": spec,
        "status": {
            "state": "Running"
        }
    }

    # Store the resource
    MEShes[name] = resource
    save_meshes(MEShes)

    return True, resource, []


def get_mesh_summaries() -> List[Dict[str, Any]]:
    """Get summaries of all meshes, sorted by name."""
    summaries = []

    for name, resource in MEShes.items():
        summaries.append({
            "name": name,
            "status": resource["status"]
        })

    # Sort by name ascending, lexicographic, case-sensitive
    summaries.sort(key=lambda x: x['name'])

    return summaries


def get_mesh_full(name: str) -> Tuple[bool, Dict[str, Any], List[Dict[str, str]]]:
    """
    Get a mesh by name.
    Returns (success, resource, errors).
    """
    if name not in MEShes:
        return False, {}, [{
            "field": "metadata.name",
            "message": f"mesh '{name}' not found",
            "type": "not_found"
        }]

    return True, MEShes[name], []


def delete_mesh(name: str) -> Tuple[bool, Dict[str, Any], List[Dict[str, str]]]:
    """
    Delete a mesh by name.
    Returns (success, confirmation, errors).
    """
    if name not in MEShes:
        return False, {}, [{
            "field": "metadata.name",
            "message": f"mesh '{name}' not found",
            "type": "not_found"
        }]

    del MEShes[name]
    save_meshes(MEShes)

    return True, {
        "message": f"mesh '{name}' deleted",
        "metadata": {"name": name}
    }, []


def load_yaml_file(filepath: str) -> Tuple[Any, List[Dict[str, str]]]:
    """
    Load and parse a YAML file.
    Returns (data, errors).
    """
    if not os.path.exists(filepath):
        return None, [{
            "field": "",
            "message": f"file '{filepath}' not found",
            "type": "parse"
        }]

    try:
        with open(filepath, 'r') as f:
            data = yaml.safe_load(f)
            return data, []
    except yaml.YAMLError as e:
        return None, [{
            "field": "",
            "message": f"failed to parse YAML: {e}",
            "type": "parse"
        }]
    except Exception as e:
        return None, [{
            "field": "",
            "message": f"failed to read file: {e}",
            "type": "parse"
        }]


def output_json(obj: Any) -> None:
    """Print JSON to stdout."""
    print(json.dumps(obj, indent=None, separators=(',', ':')))


def output_errors(errors: List[Dict[str, str]]) -> None:
    """Print errors as JSON to stdout."""
    output_json({"errors": errors})


def cmd_create(filepath: str) -> None:
    """Handle create command."""
    data, errors = load_yaml_file(filepath)

    if errors:
        output_errors(errors)
        return

    success, resource, errors = create_resource(data)

    if errors:
        output_errors(errors)
        return

    output_json(resource)


def cmd_list() -> None:
    """Handle list command."""
    summaries = get_mesh_summaries()
    output_json(summaries)


def cmd_describe(name: str) -> None:
    """Handle describe command."""
    success, resource, errors = get_mesh_full(name)

    if errors:
        output_errors(errors)
        return

    output_json(resource)


def cmd_delete(name: str) -> None:
    """Handle delete command."""
    success, confirmation, errors = delete_mesh(name)

    if errors:
        output_errors(errors)
        return

    output_json(confirmation)


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        prog='meshctl',
        description='Manage mesh resources from YAML specs'
    )

    subparsers = parser.add_subparsers(dest='command', help='Commands')

    # mesh subcommand
    mesh_parser = subparsers.add_parser('mesh', help='Mesh resource operations')
    mesh_subparsers = mesh_parser.add_subparsers(dest='operation', help='Operations')

    # create
    create_parser = mesh_subparsers.add_parser('create', help='Create a mesh from YAML')
    create_parser.add_argument('-f', '--file', required=True, help='YAML file path')

    # list
    mesh_subparsers.add_parser('list', help='List all meshes')

    # describe
    describe_parser = mesh_subparsers.add_parser('describe', help='Describe a mesh')
    describe_parser.add_argument('name', help='Mesh name')

    # delete
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
        else:
            parser.print_help()
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
