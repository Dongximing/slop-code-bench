#!/usr/bin/env python3
"""System Provisioning Planner MVP - dry-run CLI for rig config validation and planning."""

import argparse
import json
import os
import sys
from pathlib import Path, PurePath
from typing import Any, Dict, List, Optional, Tuple

import yaml


# OS version ordering (oldest to newest)
OS_VERSIONS = [
    'yosemite',
    'el_capitan',
    'sierra',
    'high_sierra',
    'mojave',
    'catalina',
    'big_sur',
    'monterey',
    'ventura',
    'sonoma'
]


def version_index(version: str) -> int:
    """Get the index of a version in the OS_VERSIONS list."""
    try:
        return OS_VERSIONS.index(version)
    except ValueError:
        return -1


def is_version_in_range(version: str, min_version: Optional[str], max_version: Optional[str]) -> bool:
    """Check if a version falls within the specified range (inclusive)."""
    version_idx = version_index(version)
    if version_idx == -1:
        return False

    min_idx = 0 if min_version is None else version_index(min_version)
    max_idx = len(OS_VERSIONS) - 1 if max_version is None else version_index(max_version)

    return min_idx <= version_idx <= max_idx


def parse_config_file(file_path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Parse a YAML or JSON config file.

    Returns:
        Tuple of (parsed_data, error_message). If successful, error_message is None.
    """
    try:
        with open(file_path, 'r') as f:
            content = f.read()

        # Try JSON first for .json files
        if file_path.suffix == '.json':
            try:
                data = json.loads(content)
                return data, None
            except json.JSONDecodeError as e:
                return None, f"parse error: {str(e)}"

        # Try YAML for .yaml and .yml files
        try:
            data = yaml.safe_load(content)
            if data is None:
                data = {}
            return data, None
        except yaml.YAMLError as e:
            return None, f"parse error: {str(e)}"
    except Exception as e:
        return None, f"parse error: {str(e)}"


def discover_config_files(base_dir: Path) -> List[Path]:
    """Discover all potential config files in the directory tree.

    Skips .git directories and only returns .yaml, .yml, or .json files.
    """
    config_files = []

    for root, dirs, files in os.walk(base_dir):
        # Skip .git directories
        if '.git' in dirs:
            dirs.remove('.git')

        for file in files:
            if file.endswith(('.yaml', '.yml', '.json')):
                config_files.append(Path(root) / file)

    return sorted(config_files)


def is_rig_config(data: Dict[str, Any]) -> bool:
    """Check if parsed data is a rig config (has top-level version)."""
    return 'version' in data


def validate_string_field(value: Any, field_name: str) -> Optional[str]:
    """Validate that a field is a string."""
    if not isinstance(value, str):
        return f"{field_name}: must be a string"
    return None


def validate_list_of_strings(value: Any, field_name: str) -> Tuple[bool, Optional[str]]:
    """Validate that a field is a list of non-empty strings.

    Returns:
        Tuple of (is_valid_list, error_message).
    """
    if not isinstance(value, list):
        return False, f"{field_name}: must be a list"

    for i, item in enumerate(value):
        if not isinstance(item, str):
            return False, f"{field_name}[{i}]: must be a string"
        if not item:
            return False, f"{field_name}[{i}]: must be non-empty"

    return True, None


def validate_preferences_config(data: Dict[str, Any]) -> List[str]:
    """Validate a preferences config according to the schema.

    Returns a list of error messages.
    """
    errors = []

    # version validation
    if 'version' not in data:
        errors.append("version: required")
    else:
        version = data['version']
        if not isinstance(version, str):
            errors.append("version: must be a string")
        elif version != "1":
            errors.append(f"unsupported version '{version}'")

    # schema validation
    if 'schema' not in data:
        errors.append("schema: required")
    else:
        schema = data['schema']
        if not isinstance(schema, str):
            errors.append("schema: must be a string")

    # preferences validation
    if 'preferences' not in data:
        errors.append("preferences: required")
    elif not isinstance(data['preferences'], list):
        errors.append("preferences: must be a list")
    else:
        for i, pref in enumerate(data['preferences']):
            if not isinstance(pref, dict):
                errors.append(f"preferences[{i}]: must be an object")
                continue

            # Required fields
            for field in ['name', 'domain', 'key', 'value', 'value_type', 'apply_command']:
                if field not in pref:
                    errors.append(f"preferences[{i}].{field}: required")

            # value_type validation
            if 'value_type' in pref:
                value_type = pref['value_type']
                if not isinstance(value_type, str):
                    errors.append(f"preferences[{i}].value_type: must be a string")
                elif value_type not in ('bool', 'int', 'string', 'float'):
                    errors.append(f"preferences[{i}].value_type: must be 'bool', 'int', 'string', or 'float', got '{value_type}'")

            # min_version validation
            if 'min_version' in pref:
                min_ver = pref['min_version']
                if not isinstance(min_ver, str):
                    errors.append(f"preferences[{i}].min_version: must be a string")
                elif version_index(min_ver) == -1:
                    errors.append(f"preferences[{i}].min_version: unknown version '{min_ver}'")

            # max_version validation
            if 'max_version' in pref:
                max_ver = pref['max_version']
                if not isinstance(max_ver, str):
                    errors.append(f"preferences[{i}].max_version: must be a string")
                elif version_index(max_ver) == -1:
                    errors.append(f"preferences[{i}].max_version: unknown version '{max_ver}'")

            # min_version <= max_version check
            if 'min_version' in pref and 'max_version' in pref:
                min_ver = pref['min_version']
                max_ver = pref['max_version']
                if isinstance(min_ver, str) and isinstance(max_ver, str):
                    min_idx = version_index(min_ver)
                    max_idx = version_index(max_ver)
                    if min_idx != -1 and max_idx != -1 and min_idx > max_idx:
                        errors.append(f"preferences[{i}]: min_version '{min_ver}' is later than max_version '{max_ver}'")

    return errors


def validate_dock_config(data: Dict[str, Any]) -> List[str]:
    """Validate a dock config according to the schema.

    Returns a list of error messages.
    """
    errors = []

    # version validation
    if 'version' not in data:
        errors.append("version: required")
    else:
        version = data['version']
        if not isinstance(version, str):
            errors.append("version: must be a string")
        elif version != "1":
            errors.append(f"unsupported version '{version}'")

    # schema validation
    if 'schema' not in data:
        errors.append("schema: required")
    else:
        schema = data['schema']
        if not isinstance(schema, str):
            errors.append("schema: must be a string")

    # os validation
    if 'os' not in data:
        errors.append("dock: 'os' is required and must be 'macos'")
    else:
        os_val = data['os']
        if not isinstance(os_val, str):
            errors.append("dock: 'os' is required and must be 'macos'")
        elif os_val != 'macos':
            errors.append("dock: 'os' is required and must be 'macos'")

    # items validation
    if 'items' not in data:
        errors.append("items: required")
    else:
        items = data['items']
        if not isinstance(items, list):
            errors.append("items: must be a list")
        elif len(items) == 0:
            errors.append("items: must be non-empty")
        else:
            for i, item in enumerate(items):
                if not isinstance(item, str):
                    errors.append(f"items[{i}]: must be a string")

    return errors


def validate_module_config(data: Dict[str, Any], file_path: Path, base_dir: Path, module_dirs: Dict[str, List[Path]]) -> List[str]:
    """Validate a module config according to the schema.

    Returns a list of error messages.
    """
    errors = []

    # 2. version validation
    if 'version' not in data:
        errors.append("version: required")
    else:
        version = data['version']
        if not isinstance(version, str):
            errors.append("version: must be a string")
        elif version != "1":
            errors.append(f"unsupported version '{version}'")

    # 3. schema validation
    if 'schema' not in data:
        errors.append("schema: required")
    else:
        schema = data['schema']
        if not isinstance(schema, str):
            errors.append("schema: must be a string")
        elif schema not in ('module', 'preferences', 'dock'):
            errors.append(f"unknown schema '{schema}'")

    # If schema is not "module", we can skip the rest of validation
    if 'schema' in data and data.get('schema') != "module":
        # Still check for duplicate module schema
        return errors

    # If schema is missing or not a string, we can't determine if it's a module
    # But we should still continue validation for other fields if possible

    # 4. os type check
    os_config = data.get('os')
    if os_config is not None:
        if not isinstance(os_config, dict):
            errors.append("os: must be an object")
            os_config = None

    if os_config is not None:
        # 5. os.name validation
        if 'name' not in os_config:
            errors.append("os.name: required")
        else:
            os_name = os_config['name']
            if not isinstance(os_name, str):
                errors.append("os.name: must be a string")
            elif os_name not in ('macos', 'linux'):
                errors.append(f"os.name: must be 'macos' or 'linux', got '{os_name}'")

        # Check if packages or applications exist
        has_packages = 'packages' in os_config
        has_applications = 'applications' in os_config

        # 6. os.package_manager validation
        if has_packages or has_applications:
            if 'package_manager' not in os_config:
                errors.append("os.package_manager: required when packages or applications present")
            else:
                pm = os_config['package_manager']
                if not isinstance(pm, str):
                    errors.append("os.package_manager: must be a string")
                elif pm not in ('brew', 'apt', 'yum'):
                    errors.append(f"os.package_manager: must be 'brew', 'apt', or 'yum', got '{pm}'")

        # 7. os.packages validation
        if 'packages' in os_config:
            valid, err = validate_list_of_strings(os_config['packages'], 'os.packages')
            if not valid:
                errors.append(err)

        # 8. os.applications validation
        if 'applications' in os_config:
            valid, err = validate_list_of_strings(os_config['applications'], 'os.applications')
            if not valid:
                errors.append(err)

    # 9. actions validation
    actions = data.get('actions')
    if actions is not None:
        if not isinstance(actions, list):
            errors.append("actions: must be a list")
        else:
            for i, action in enumerate(actions):
                if not isinstance(action, dict):
                    errors.append(f"actions[{i}]: must be an object")
                    continue

                # 10. actions[N].type validation
                if 'type' not in action:
                    errors.append(f"actions[{i}].type: required")
                else:
                    action_type = action['type']
                    if not isinstance(action_type, str):
                        errors.append(f"actions[{i}].type: must be a string")
                    elif action_type not in ('link', 'copy', 'run'):
                        errors.append(f"actions[{i}].type: unknown type '{action_type}'")

                # 11. actions[N].source validation
                if 'source' not in action:
                    errors.append(f"actions[{i}].source: required")
                else:
                    source = action['source']
                    if not isinstance(source, str):
                        errors.append(f"actions[{i}].source: must be a string")
                    elif not source:
                        errors.append(f"actions[{i}].source: must be non-empty")
                    elif source != "*" and source.startswith('/'):
                        errors.append(f"actions[{i}].source: must be module-relative (not start with '/')")

                # 12. actions[N].destination validation
                action_type = action.get('type')
                source = action.get('source')
                has_destination = 'destination' in action

                if action_type in ('link', 'copy'):
                    if not has_destination:
                        errors.append(f"actions[{i}].destination: required for link/copy")
                    else:
                        dest = action['destination']
                        if not isinstance(dest, str):
                            errors.append(f"actions[{i}].destination: must be a string")
                        elif not (dest.startswith('/') or dest.startswith('~')):
                            errors.append(f"actions[{i}].destination: must start with / or ~")
                        elif source == "*" and not dest.endswith('/'):
                            errors.append(f"actions[{i}].destination: must end with / for wildcard source")
                elif action_type == 'run':
                    if has_destination:
                        dest = action['destination']
                        if not isinstance(dest, str):
                            errors.append(f"actions[{i}].destination: must be a string")
                        elif not (dest.startswith('/') or dest.startswith('~')):
                            errors.append(f"actions[{i}].destination: must start with / or ~")

                # 13. actions[N].hidden and elevated validation
                if 'hidden' in action:
                    if not isinstance(action['hidden'], bool):
                        errors.append(f"actions[{i}].hidden: must be a boolean")

                if 'elevated' in action:
                    if not isinstance(action['elevated'], bool):
                        errors.append(f"actions[{i}].elevated: must be a boolean")

    return errors


def get_module_name(file_path: Path, base_dir: Path) -> Optional[str]:
    """Get the module name (directory path relative to base_dir).

    Returns None if the file is at root level.
    """
    rel_path = file_path.relative_to(base_dir)
    parts = rel_path.parts

    if len(parts) <= 1:
        # Root level file
        return None

    # Module name is the directory path
    return '/'.join(parts[:-1])


def validate_all_configs(base_dir: Path) -> Dict[str, Any]:
    """Validate all config files in the directory.

    Returns the validation result as a dict.
    """
    config_files = discover_config_files(base_dir)
    file_results = []
    all_valid = True

    # Track module directories for duplicate detection
    module_dirs: Dict[str, List[Path]] = {}

    # Track dock configs for uniqueness check
    dock_configs: List[Path] = []

    # First pass: parse all files and collect module configs
    parsed_configs = []
    for file_path in config_files:
        rel_path = file_path.relative_to(base_dir)
        data, parse_error = parse_config_file(file_path)

        if parse_error:
            file_results.append({
                "path": str(rel_path),
                "valid": False,
                "errors": [parse_error]
            })
            all_valid = False
            continue

        if not is_rig_config(data):
            # Not a rig config, skip
            continue

        parsed_configs.append((file_path, data))

        # Collect dock configs
        if data.get('schema') == 'dock':
            dock_configs.append(file_path)

    # Check for multiple dock configs
    if len(dock_configs) > 1:
        all_valid = False
        for dock_file in dock_configs:
            rel_path = dock_file.relative_to(base_dir)
            # Find existing result or add new one
            found = False
            for result in file_results:
                if result['path'] == str(rel_path):
                    if 'errors' not in result:
                        result['errors'] = []
                    result['valid'] = False
                    result['errors'].append("multiple 'dock' schemas found — only one is allowed")
                    found = True
                    break
            if not found:
                file_results.append({
                    "path": str(rel_path),
                    "valid": False,
                    "errors": ["multiple 'dock' schemas found — only one is allowed"]
                })

    # Collect module configs for duplicate detection
    for file_path, data in parsed_configs:
        schema = data.get('schema')
        if schema == "module":
            module_name = get_module_name(file_path, base_dir)
            if module_name:
                if module_name not in module_dirs:
                    module_dirs[module_name] = []
                module_dirs[module_name].append(file_path)

    # Second pass: validate and check for duplicates
    for file_path, data in parsed_configs:
        rel_path = file_path.relative_to(base_dir)
        schema = data.get('schema')

        # Skip validation if already marked as invalid for multiple docks
        skip_validation = False
        for result in file_results:
            if result['path'] == str(rel_path) and not result['valid']:
                skip_validation = True
                break

        if skip_validation:
            continue

        errors = []
        if schema == 'module':
            errors = validate_module_config(data, file_path, base_dir, module_dirs)
        elif schema == 'preferences':
            errors = validate_preferences_config(data)
        elif schema == 'dock':
            errors = validate_dock_config(data)
        else:
            # Unknown schema or missing schema - validate as module for base checks
            errors = validate_module_config(data, file_path, base_dir, module_dirs)

        # Check for duplicate module schema
        module_name = get_module_name(file_path, base_dir)
        if module_name and module_name in module_dirs and len(module_dirs[module_name]) > 1:
            if data.get('schema') == "module":
                dup_error = f"duplicate 'module' schema in '{module_name}'"
                if dup_error not in errors:
                    errors.append(dup_error)

        if errors:
            file_results.append({
                "path": str(rel_path),
                "valid": False,
                "errors": errors
            })
            all_valid = False
        else:
            file_results.append({
                "path": str(rel_path),
                "valid": True
            })

    # Sort file results by path
    file_results.sort(key=lambda x: x["path"])

    result = {
        "valid": all_valid,
        "files": file_results
    }

    return result


def expand_wildcard_source(module_dir: Path, destination: str, hidden: bool, elevated: bool, action_type: str) -> List[Dict[str, Any]]:
    """Expand wildcard source to concrete file actions.

    Returns a list of action dicts.
    """
    actions = []

    # Get all files in module directory
    try:
        entries = sorted(os.listdir(module_dir))
    except OSError:
        return []

    for entry in entries:
        entry_path = module_dir / entry

        # Exclude directories
        if entry_path.is_dir():
            continue

        # Exclude config files
        if entry.endswith(('.yaml', '.yml', '.json')):
            continue

        # Exclude hidden files
        if entry.startswith('.'):
            continue

        # Build destination path
        dest_filename = entry
        if hidden and not dest_filename.startswith('.'):
            dest_filename = '.' + dest_filename

        # Resolve paths
        source_abs = str(entry_path.resolve())
        dest_abs = resolve_destination(destination + dest_filename)

        if action_type == 'run':
            action = {
                "type": "run",
                "source": source_abs,
                "elevated": elevated
            }
        else:
            action = {
                "type": action_type,
                "source": source_abs,
                "destination": dest_abs,
                "elevated": elevated
            }

        actions.append(action)

    return actions


def resolve_destination(dest: str) -> str:
    """Resolve destination path, expanding ~ to $HOME."""
    if dest.startswith('~'):
        home = os.environ.get('HOME', os.path.expanduser('~'))
        return os.path.join(home, dest[1:].lstrip('/'))
    return dest


def detect_conflicts(file_actions: List[Tuple[str, str, str, str]]) -> Optional[Dict[str, Any]]:
    """Detect conflicts where multiple actions target the same destination.

    Args:
        file_actions: List of (module, action_type, source, destination) tuples

    Returns:
        Conflict dict if conflicts found, None otherwise
    """
    # Group actions by destination
    dest_to_sources: Dict[str, List[Tuple[str, str, str]]] = {}  # dest -> [(module, type, source)]

    for module, action_type, source, destination in file_actions:
        if destination not in dest_to_sources:
            dest_to_sources[destination] = []
        dest_to_sources[destination].append((module, action_type, source))

    # Find conflicts (destinations with more than one source)
    conflicts = []
    for dest, sources in dest_to_sources.items():
        if len(sources) > 1:
            # Sort sources by module, then source, then type
            sorted_sources = sorted(sources, key=lambda x: (x[0], x[2], x[1]))
            conflict_sources = []
            for module, action_type, source in sorted_sources:
                conflict_sources.append({
                    "module": module,
                    "type": action_type,
                    "source": source
                })
            conflicts.append({
                "destination": dest,
                "sources": conflict_sources
            })

    if conflicts:
        # Sort conflicts by destination
        conflicts.sort(key=lambda x: x['destination'])
        return {
            "error": "conflict",
            "conflicts": conflicts
        }

    return None


def generate_plan(base_dir: Path, os_filter: Optional[str] = None, module_filter: Optional[List[str]] = None, os_version_filter: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Generate the provisioning plan.

    Returns:
        Tuple of (plan, error). On success, plan is returned and error is None.
        On failure, plan is None and error is returned.
    """
    # Check os_version_filter is valid
    if os_version_filter is not None:
        if version_index(os_version_filter) == -1:
            return None, {
                "error": "unknown_os_version",
                "details": f"unknown version '{os_version_filter}'"
            }

    # First validate
    validation_result = validate_all_configs(base_dir)

    if not validation_result["valid"]:
        return None, {
            "error": "validation_failed",
            "details": validation_result
        }

    # Collect all configs from all module directories
    config_files = discover_config_files(base_dir)

    # Group configs by module directory
    module_configs: Dict[str, Dict[str, Any]] = {}  # module_name -> module data
    module_preferences: Dict[str, List[Dict[str, Any]]] = {}  # module_name -> list of preferences
    module_dock: Optional[Tuple[str, Dict[str, Any]]] = None  # (module_name, dock_data)

    for file_path in config_files:
        data, parse_error = parse_config_file(file_path)

        if parse_error or not is_rig_config(data):
            continue

        module_name = get_module_name(file_path, base_dir)
        if not module_name:
            continue

        schema = data.get('schema')

        if schema == 'module':
            module_configs[module_name] = data
        elif schema == 'preferences':
            if module_name not in module_preferences:
                module_preferences[module_name] = []
            # Collect all preferences from all preferences configs in this module
            for pref in data.get('preferences', []):
                module_preferences[module_name].append(pref)
        elif schema == 'dock':
            # There should only be one dock config (validated earlier)
            module_dock = (module_name, data)

    # Build module list from all discovered modules
    all_module_names = set(module_configs.keys()) | set(module_preferences.keys())
    if module_dock:
        all_module_names.add(module_dock[0])

    # If no modules found, check for configs
    if not all_module_names:
        has_any_config = False
        for file_path in config_files:
            data, parse_error = parse_config_file(file_path)
            if parse_error:
                has_any_config = True
                break
            if is_rig_config(data):
                has_any_config = True
                break

        if not has_any_config:
            return None, {
                "error": "no_configs",
                "details": "no rig config files discovered"
            }

    # Filter modules by OS and name
    filtered_modules = []

    for module_name in all_module_names:
        module_data = module_configs.get(module_name, {})

        # OS filter
        if os_filter is not None:
            os_config = module_data.get('os')
            if os_config and isinstance(os_config, dict):
                module_os = os_config.get('name')
                if module_os and module_os != os_filter:
                    continue

        # Module name filter
        if module_filter is not None:
            if module_name not in module_filter:
                continue

        filtered_modules.append(module_name)

    # Sort modules alphabetically
    filtered_modules.sort()

    # Collect all link/copy actions for conflict detection
    all_file_actions: List[Tuple[str, str, str, str]] = []  # (module, action_type, source, destination)

    # Generate plan
    plan_modules = []

    for module_name in filtered_modules:
        module_data = module_configs.get(module_name, {})
        module_dir_base = base_dir / module_name

        # Find the actual module directory (where module.yaml is)
        # or just use base_dir / module_name if no module.yaml
        module_dir = module_dir_base
        for file_path in config_files:
            if get_module_name(file_path, base_dir) == module_name:
                if parse_config_file(file_path)[0] and parse_config_file(file_path)[0].get('schema') == 'module':
                    module_dir = file_path.parent
                    break

        actions = []

        # 1. install_package actions (sorted alphabetically)
        os_config = module_data.get('os')
        if os_config and isinstance(os_config, dict):
            pm = os_config.get('package_manager')

            packages = os_config.get('packages', [])
            if packages:
                for pkg in sorted(packages):
                    actions.append({
                        "type": "install_package",
                        "manager": pm,
                        "package": pkg
                    })

            # 2. install_application actions (sorted alphabetically)
            applications = os_config.get('applications', [])
            if applications:
                for app in sorted(applications):
                    actions.append({
                        "type": "install_application",
                        "manager": pm,
                        "application": app
                    })

        # 3. File actions (link, copy, run) in config order
        config_actions = module_data.get('actions', [])

        for action in config_actions:
            action_type = action.get('type')
            source = action.get('source', '')
            destination = action.get('destination')
            hidden = action.get('hidden', False)
            elevated = action.get('elevated', False)

            if source == "*":
                # Wildcard expansion
                if destination:
                    expanded = expand_wildcard_source(
                        module_dir, destination, hidden, elevated, action_type
                    )
                    for exp_action in expanded:
                        if action_type in ('link', 'copy'):
                            all_file_actions.append((module_name, action_type, exp_action['source'], exp_action['destination']))
                        actions.append(exp_action)
            else:
                # Non-wildcard action
                source_abs = str((module_dir / source).resolve())

                # Check if source file exists
                if not os.path.exists(source_abs):
                    return None, {
                        "error": "file_not_found",
                        "details": f"module '{module_name}', missing source file '{source}'"
                    }

                if action_type == 'run':
                    actions.append({
                        "type": "run",
                        "source": source_abs,
                        "elevated": elevated
                    })
                else:
                    if destination:
                        dest_abs = resolve_destination(destination)

                        if hidden:
                            pp = PurePath(dest_abs)
                            filename = pp.name
                            if not filename.startswith('.'):
                                dest_abs = str(pp.parent / ('.' + filename))

                        all_file_actions.append((module_name, action_type, source_abs, dest_abs))
                        actions.append({
                            "type": action_type,
                            "source": source_abs,
                            "destination": dest_abs,
                            "elevated": elevated
                        })

        # 4. set_preference actions in order from preferences configs
        prefs = module_preferences.get(module_name, [])
        for pref in prefs:
            # Check enabled
            if pref.get('enabled') is False:
                continue

            # Check version range
            if os_version_filter is not None:
                min_ver = pref.get('min_version')
                max_ver = pref.get('max_version')
                if not is_version_in_range(os_version_filter, min_ver, max_ver):
                    continue

            action = {
                "type": "set_preference",
                "name": pref['name'],
                "domain": pref['domain'],
                "key": pref['key'],
                "value": pref['value'],
                "value_type": pref['value_type'],
                "apply_command": pref['apply_command']
            }
            if 'check_command' in pref:
                action['check_command'] = pref['check_command']
            if 'expected_state' in pref:
                action['expected_state'] = pref['expected_state']
            actions.append(action)

        # 5. configure_dock action (at most one)
        if module_dock and module_dock[0] == module_name:
            dock_data = module_dock[1]
            # Apply OS filter for dock
            if os_filter is None or os_filter == 'macos':
                actions.append({
                    "type": "configure_dock",
                    "items": dock_data.get('items', [])
                })

        plan_modules.append({
            "name": module_name,
            "actions": actions
        })

    # Check for conflicts
    conflicts = detect_conflicts(all_file_actions)
    if conflicts:
        return None, conflicts

    return {"modules": plan_modules}, None


def cmd_validate(args):
    """Handle the validate command."""
    base_dir = Path(args.dir).resolve()

    if not base_dir.exists():
        print(json.dumps({
            "valid": False,
            "files": [],
            "error": "directory not found"
        }))
        sys.exit(1)

    result = validate_all_configs(base_dir)
    print(json.dumps(result, indent=2))

    if result["valid"]:
        sys.exit(0)
    else:
        sys.exit(1)


def cmd_plan(args):
    """Handle the plan command."""
    base_dir = Path(args.dir).resolve()

    if not base_dir.exists():
        print(json.dumps({
            "error": "file_not_found",
            "details": f"directory '{args.dir}' not found"
        }), file=sys.stderr)
        sys.exit(1)

    os_filter = args.os
    module_filter = args.module if args.module else None
    os_version_filter = args.os_version

    plan, error = generate_plan(base_dir, os_filter, module_filter, os_version_filter)

    if error:
        print(json.dumps(error), file=sys.stderr)
        sys.exit(1)

    print(json.dumps(plan, indent=2))
    sys.exit(0)


def main():
    parser = argparse.ArgumentParser(description='System Provisioning Planner MVP')
    subparsers = parser.add_subparsers(dest='command', help='Commands')

    # validate command
    validate_parser = subparsers.add_parser('validate', help='Validate config files')
    validate_parser.add_argument('dir', help='Directory to validate')

    # plan command
    plan_parser = subparsers.add_parser('plan', help='Generate provisioning plan')
    plan_parser.add_argument('dir', help='Directory to plan')
    plan_parser.add_argument('--os', dest='os', help='Filter by OS (macos/linux)')
    plan_parser.add_argument('--module', dest='module', nargs='+', help='Filter by module name')
    plan_parser.add_argument('--os-version', dest='os_version', help='Filter by OS version for preferences')

    args = parser.parse_args()

    if args.command == 'validate':
        cmd_validate(args)
    elif args.command == 'plan':
        cmd_plan(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
