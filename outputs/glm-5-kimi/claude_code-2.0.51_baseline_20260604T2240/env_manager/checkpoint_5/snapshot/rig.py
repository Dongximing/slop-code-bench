#!/usr/bin/env python3
"""
System Provisioning Planner MVP

A dry-run CLI that discovers rig config files, validates them, and generates
a deterministic plan for package installs, app installs, and file actions.
"""

import json
import os
import shutil
import stat
import sys
import tarfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
import yaml


# OS version ordering from oldest to newest
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
    """Get the index of a version in the ordered list. Returns -1 if not found."""
    try:
        return OS_VERSIONS.index(version)
    except ValueError:
        return -1


def is_version_in_range(version: str, min_version: Optional[str], max_version: Optional[str]) -> bool:
    """Check if a version falls within the specified range (inclusive)."""
    v_idx = version_index(version)
    if v_idx == -1:
        return False

    if min_version is not None:
        min_idx = version_index(min_version)
        if min_idx == -1 or v_idx < min_idx:
            return False

    if max_version is not None:
        max_idx = version_index(max_version)
        if max_idx == -1 or v_idx > max_idx:
            return False

    return True


def discover_configs(directory: str) -> List[Dict[str, Any]]:
    """
    Recursively walk directory and discover rig config files.

    Returns list of dicts with:
    - path: relative path from directory
    - abs_path: absolute path
    - content: parsed content or None if parse error
    - parse_error: error message or None
    """
    configs = []
    base_path = Path(directory).resolve()

    for root, dirs, files in os.walk(str(base_path)):
        # Skip .git directories
        if '.git' in dirs:
            dirs.remove('.git')

        for filename in files:
            if not (filename.endswith('.yaml') or filename.endswith('.yml') or filename.endswith('.json')):
                continue

            file_path = Path(root) / filename
            rel_path = str(file_path.relative_to(base_path))

            try:
                with open(file_path, 'r') as f:
                    if filename.endswith('.json'):
                        content = json.load(f)
                    else:
                        content = yaml.safe_load(f)

                # Only include if it has top-level 'version'
                if isinstance(content, dict) and 'version' in content:
                    configs.append({
                        'path': rel_path,
                        'abs_path': str(file_path.resolve()),
                        'content': content,
                        'parse_error': None
                    })
            except Exception as e:
                # Parse error - still include as it's a rig config attempt
                configs.append({
                    'path': rel_path,
                    'abs_path': str(file_path.resolve()),
                    'content': None,
                    'parse_error': str(e)
                })

    # Sort by path
    configs.sort(key=lambda x: x['path'])
    return configs


def validate_depends_on(config: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """
    Validate depends_on field if present.

    Returns (is_valid, list_of_errors).
    """
    errors = []

    if 'depends_on' in config:
        depends_on = config['depends_on']
        if not isinstance(depends_on, list):
            errors.append("depends_on: must be a list")
        else:
            for i, dep in enumerate(depends_on):
                if not isinstance(dep, str):
                    errors.append(f"depends_on[{i}]: must be a string")
                elif dep == '':
                    errors.append(f"depends_on[{i}]: must be non-empty")

    return (len(errors) == 0, errors)


def validate_module(config: Dict[str, Any], module_dir: Path) -> Tuple[bool, List[str]]:
    """
    Validate a module config.

    Returns (is_valid, list_of_errors).
    """
    errors = []

    # Validation order is critical per spec
    # Step 2: version validation
    if 'version' not in config:
        errors.append("version: required")
    elif not isinstance(config['version'], str):
        errors.append("version: must be a string")
    elif config['version'] != '1':
        errors.append(f"unsupported version '{config['version']}'")

    # Step 3: schema validation
    if 'schema' not in config:
        errors.append("schema: required")
    elif not isinstance(config['schema'], str):
        errors.append("schema: must be a string")
    elif config['schema'] not in ('module', 'preferences', 'dock', 'environments'):
        errors.append(f"unknown schema '{config['schema']}'")

    # Early exit if schema is not module
    if config.get('schema') != 'module':
        # Still validate depends_on for non-module schemas
        depends_valid, depends_errors = validate_depends_on(config)
        errors.extend(depends_errors)
        return (len(errors) == 0, errors)

    # Validate depends_on field
    depends_valid, depends_errors = validate_depends_on(config)
    errors.extend(depends_errors)

    # Step 4: os type check
    if 'os' in config:
        if not isinstance(config['os'], dict):
            errors.append("os: must be an object")
        else:
            os_config = config['os']

            # Step 5: os.name validation
            if 'name' not in os_config:
                errors.append("os.name: required")
            elif not isinstance(os_config['name'], str):
                errors.append("os.name: must be a string")
            elif os_config['name'] not in ('macos', 'linux'):
                errors.append(f"os.name: unsupported value '{os_config['name']}'")

            # Step 6: os.package_manager validation
            has_packages = 'packages' in os_config
            has_applications = 'applications' in os_config

            if has_packages or has_applications:
                if 'package_manager' not in os_config:
                    errors.append("os.package_manager: required when packages or applications present")
                elif not isinstance(os_config['package_manager'], str):
                    errors.append("os.package_manager: must be a string")
                elif os_config['package_manager'] not in ('brew', 'apt', 'yum'):
                    errors.append(f"os.package_manager: unsupported value '{os_config['package_manager']}'")

            # Step 7: os.packages validation
            if has_packages:
                if not isinstance(os_config['packages'], list):
                    errors.append("os.packages: must be a list")
                else:
                    for i, pkg in enumerate(os_config['packages']):
                        if not isinstance(pkg, str):
                            errors.append(f"os.packages[{i}]: must be a string")
                        elif pkg == '':
                            errors.append(f"os.packages[{i}]: must be non-empty")

            # Step 8: os.applications validation
            if has_applications:
                if not isinstance(os_config['applications'], list):
                    errors.append("os.applications: must be a list")
                else:
                    for i, app in enumerate(os_config['applications']):
                        if not isinstance(app, str):
                            errors.append(f"os.applications[{i}]: must be a string")
                        elif app == '':
                            errors.append(f"os.applications[{i}]: must be non-empty")

    # Step 9: actions validation
    if 'actions' in config:
        if not isinstance(config['actions'], list):
            errors.append("actions: must be a list")
        else:
            for i, action in enumerate(config['actions']):
                if not isinstance(action, dict):
                    errors.append(f"actions[{i}]: must be an object")
                    continue

                # Step 10: actions[N].type validation
                if 'type' not in action:
                    errors.append(f"actions[{i}].type: required")
                elif not isinstance(action['type'], str):
                    errors.append(f"actions[{i}].type: must be a string")
                elif action['type'] not in ('link', 'copy', 'run'):
                    errors.append(f"actions[{i}].type: unknown action type '{action['type']}'")

                action_type = action.get('type')

                # Step 11: actions[N].source validation
                if 'source' not in action:
                    errors.append(f"actions[{i}].source: required")
                elif not isinstance(action['source'], str):
                    errors.append(f"actions[{i}].source: must be a string")
                elif action['source'] == '':
                    errors.append(f"actions[{i}].source: must be non-empty")
                elif action['source'] != '*' and action['source'].startswith('/'):
                    errors.append(f"actions[{i}].source: must be module-relative (not absolute)")

                # Step 12: actions[N].destination validation
                has_dest = 'destination' in action
                if action_type in ('link', 'copy'):
                    if not has_dest:
                        errors.append(f"actions[{i}].destination: required for link/copy")
                    elif not isinstance(action['destination'], str):
                        errors.append(f"actions[{i}].destination: must be a string")
                    elif not (action['destination'].startswith('/') or action['destination'].startswith('~')):
                        errors.append(f"actions[{i}].destination: must be absolute or ~-prefixed")
                    elif action.get('source') == '*' and not action['destination'].endswith('/'):
                        errors.append(f"actions[{i}].destination: must end with / for wildcard source")
                elif action_type == 'run' and has_dest:
                    if not isinstance(action['destination'], str):
                        errors.append(f"actions[{i}].destination: must be a string")
                    elif not (action['destination'].startswith('/') or action['destination'].startswith('~')):
                        errors.append(f"actions[{i}].destination: must be absolute or ~-prefixed")

                # Step 13: hidden and elevated validation
                if 'hidden' in action:
                    if not isinstance(action['hidden'], bool):
                        errors.append(f"actions[{i}].hidden: must be a boolean")

                if 'elevated' in action:
                    if not isinstance(action['elevated'], bool):
                        errors.append(f"actions[{i}].elevated: must be a boolean")

    return (len(errors) == 0, errors)


def validate_preferences(config: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """
    Validate a preferences config.

    Returns (is_valid, list_of_errors).
    """
    errors = []

    # Step 2: version validation
    if 'version' not in config:
        errors.append("version: required")
    elif not isinstance(config['version'], str):
        errors.append("version: must be a string")
    elif config['version'] != '1':
        errors.append(f"unsupported version '{config['version']}'")

    # Step 3: schema validation
    if 'schema' not in config:
        errors.append("schema: required")
    elif not isinstance(config['schema'], str):
        errors.append("schema: must be a string")
    elif config['schema'] != 'preferences':
        errors.append(f"unknown schema '{config['schema']}'")

    # Early exit if schema is not preferences
    if config.get('schema') != 'preferences':
        # Still validate depends_on
        depends_valid, depends_errors = validate_depends_on(config)
        errors.extend(depends_errors)
        return (len(errors) == 0, errors)

    # Validate depends_on field
    depends_valid, depends_errors = validate_depends_on(config)
    errors.extend(depends_errors)

    # Validate preferences list
    if 'preferences' not in config:
        errors.append("preferences: required")
    elif not isinstance(config['preferences'], list):
        errors.append("preferences: must be a list")
    else:
        valid_value_types = ('bool', 'int', 'string', 'float')

        for i, pref in enumerate(config['preferences']):
            if not isinstance(pref, dict):
                errors.append(f"preferences[{i}]: must be an object")
                continue

            # Required fields
            for field in ('name', 'domain', 'key', 'value', 'value_type', 'apply_command'):
                if field not in pref:
                    errors.append(f"preferences[{i}].{field}: required")

            # value_type validation
            if 'value_type' in pref:
                if not isinstance(pref['value_type'], str):
                    errors.append(f"preferences[{i}].value_type: must be a string")
                elif pref['value_type'] not in valid_value_types:
                    errors.append(f"preferences[{i}].value_type: must be 'bool', 'int', 'string', or 'float', got '{pref['value_type']}'")

            # min_version validation
            if 'min_version' in pref:
                if not isinstance(pref['min_version'], str):
                    errors.append(f"preferences[{i}].min_version: must be a string")
                elif version_index(pref['min_version']) == -1:
                    errors.append(f"preferences[{i}].min_version: unknown version '{pref['min_version']}'")

            # max_version validation
            if 'max_version' in pref:
                if not isinstance(pref['max_version'], str):
                    errors.append(f"preferences[{i}].max_version: must be a string")
                elif version_index(pref['max_version']) == -1:
                    errors.append(f"preferences[{i}].max_version: unknown version '{pref['max_version']}'")

            # Check min_version <= max_version
            if 'min_version' in pref and 'max_version' in pref:
                min_idx = version_index(pref['min_version'])
                max_idx = version_index(pref['max_version'])
                if min_idx != -1 and max_idx != -1 and min_idx > max_idx:
                    errors.append(f"preferences[{i}]: min_version '{pref['min_version']}' is later than max_version '{pref['max_version']}'")

            # enabled validation
            if 'enabled' in pref:
                if not isinstance(pref['enabled'], bool):
                    errors.append(f"preferences[{i}].enabled: must be a boolean")

    return (len(errors) == 0, errors)


def validate_dock(config: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """
    Validate a dock config.

    Returns (is_valid, list_of_errors).
    """
    errors = []

    # Step 2: version validation
    if 'version' not in config:
        errors.append("version: required")
    elif not isinstance(config['version'], str):
        errors.append("version: must be a string")
    elif config['version'] != '1':
        errors.append(f"unsupported version '{config['version']}'")

    # Step 3: schema validation
    if 'schema' not in config:
        errors.append("schema: required")
    elif not isinstance(config['schema'], str):
        errors.append("schema: must be a string")
    elif config['schema'] != 'dock':
        errors.append(f"unknown schema '{config['schema']}'")

    # Early exit if schema is not dock
    if config.get('schema') != 'dock':
        # Still validate depends_on
        depends_valid, depends_errors = validate_depends_on(config)
        errors.extend(depends_errors)
        return (len(errors) == 0, errors)

    # Validate depends_on field
    depends_valid, depends_errors = validate_depends_on(config)
    errors.extend(depends_errors)

    # os validation - required and must be "macos"
    if 'os' not in config:
        errors.append("dock: 'os' is required and must be 'macos'")
    elif not isinstance(config['os'], str):
        errors.append("dock: 'os' is required and must be 'macos'")
    elif config['os'] != 'macos':
        errors.append("dock: 'os' is required and must be 'macos'")

    # items validation - required and must be non-empty list of strings
    if 'items' not in config:
        errors.append("dock.items: required")
    elif not isinstance(config['items'], list):
        errors.append("dock.items: must be a list")
    elif len(config['items']) == 0:
        errors.append("dock.items: must be non-empty")
    else:
        for i, item in enumerate(config['items']):
            if not isinstance(item, str):
                errors.append(f"dock.items[{i}]: must be a string")
            elif item == '':
                errors.append(f"dock.items[{i}]: must be non-empty")

    return (len(errors) == 0, errors)


def validate_environments(config: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """
    Validate an environments config.

    Returns (is_valid, list_of_errors).
    """
    errors = []

    # Step 2: version validation
    if 'version' not in config:
        errors.append("version: required")
    elif not isinstance(config['version'], str):
        errors.append("version: must be a string")
    elif config['version'] != '1':
        errors.append(f"unsupported version '{config['version']}'")

    # Step 3: schema validation
    if 'schema' not in config:
        errors.append("schema: required")
    elif not isinstance(config['schema'], str):
        errors.append("schema: must be a string")
    elif config['schema'] != 'environments':
        errors.append(f"unknown schema '{config['schema']}'")

    # Early exit if schema is not environments
    if config.get('schema') != 'environments':
        # Still validate depends_on
        depends_valid, depends_errors = validate_depends_on(config)
        errors.extend(depends_errors)
        return (len(errors) == 0, errors)

    # Validate depends_on field
    depends_valid, depends_errors = validate_depends_on(config)
    errors.extend(depends_errors)

    # environments must be a non-empty list
    if 'environments' not in config:
        errors.append("environments: required")
    elif not isinstance(config['environments'], list):
        errors.append("environments: must be a list")
    elif len(config['environments']) == 0:
        errors.append("environments: must be non-empty")
    else:
        for i, env in enumerate(config['environments']):
            if not isinstance(env, dict):
                errors.append(f"environments[{i}]: must be an object")
                continue

            # language is required and must be a non-empty string
            if 'language' not in env:
                errors.append(f"environments[{i}].language: required")
            elif not isinstance(env['language'], str):
                errors.append(f"environments[{i}].language: must be a string")
            elif env['language'] == '':
                errors.append(f"environments[{i}].language: must be non-empty")

            # versions is required and must be a non-empty list of non-empty strings
            if 'versions' not in env:
                errors.append(f"environments[{i}].versions: required")
            elif not isinstance(env['versions'], list):
                errors.append(f"environments[{i}].versions: must be a list")
            elif len(env['versions']) == 0:
                errors.append(f"environments[{i}].versions: must be non-empty")
            else:
                for j, ver in enumerate(env['versions']):
                    if not isinstance(ver, str):
                        errors.append(f"environments[{i}].versions[{j}]: must be a string")
                    elif ver == '':
                        errors.append(f"environments[{i}].versions[{j}]: must be non-empty")

            # manager is required and must be an object
            if 'manager' not in env:
                errors.append(f"environments[{i}].manager: required")
            elif not isinstance(env['manager'], dict):
                errors.append(f"environments[{i}].manager: must be an object")
            else:
                manager = env['manager']

                # manager.name is required and must be a non-empty string
                if 'name' not in manager:
                    errors.append(f"environments[{i}].manager.name: required")
                elif not isinstance(manager['name'], str):
                    errors.append(f"environments[{i}].manager.name: must be a string")
                elif manager['name'] == '':
                    errors.append(f"environments[{i}].manager.name: must be non-empty")

                # plugins is optional, but when present must be a list
                if 'plugins' in manager:
                    if not isinstance(manager['plugins'], list):
                        errors.append(f"environments[{i}].manager.plugins: must be a list")
                    else:
                        for k, plugin in enumerate(manager['plugins']):
                            if not isinstance(plugin, dict):
                                errors.append(f"environments[{i}].manager.plugins[{k}]: must be an object")
                                continue

                            # plugin.name is required and must be a non-empty string
                            if 'name' not in plugin:
                                errors.append(f"environments[{i}].manager.plugins[{k}].name: required")
                            elif not isinstance(plugin['name'], str):
                                errors.append(f"environments[{i}].manager.plugins[{k}].name: must be a string")
                            elif plugin['name'] == '':
                                errors.append(f"environments[{i}].manager.plugins[{k}].name: must be non-empty")

                            # virtual_environments is optional, but when present must be a list
                            if 'virtual_environments' in plugin:
                                if not isinstance(plugin['virtual_environments'], list):
                                    errors.append(f"environments[{i}].manager.plugins[{k}].virtual_environments: must be a list")
                                else:
                                    for m, venv in enumerate(plugin['virtual_environments']):
                                        if not isinstance(venv, dict):
                                            errors.append(f"environments[{i}].manager.plugins[{k}].virtual_environments[{m}]: must be an object")
                                            continue

                                        # version is required and must match parent environment's versions
                                        if 'version' not in venv:
                                            errors.append(f"environments[{i}].manager.plugins[{k}].virtual_environments[{m}].version: required")
                                        elif not isinstance(venv['version'], str):
                                            errors.append(f"environments[{i}].manager.plugins[{k}].virtual_environments[{m}].version: must be a string")
                                        elif 'versions' in env and isinstance(env['versions'], list):
                                            if venv['version'] not in env['versions']:
                                                errors.append(f"environments[{i}].manager.plugins[{k}].virtual_environments[{m}].version: '{venv['version']}' is not in the environment's versions list")

                                        # name is required and must be a non-empty string
                                        if 'name' not in venv:
                                            errors.append(f"environments[{i}].manager.plugins[{k}].virtual_environments[{m}].name: required")
                                        elif not isinstance(venv['name'], str):
                                            errors.append(f"environments[{i}].manager.plugins[{k}].virtual_environments[{m}].name: must be a string")
                                        elif venv['name'] == '':
                                            errors.append(f"environments[{i}].manager.plugins[{k}].virtual_environments[{m}].name: must be non-empty")

    return (len(errors) == 0, errors)


def find_module_schema_file(module_dir: Path, configs: List[Dict[str, Any]]) -> Tuple[Optional[str], Optional[List[str]]]:
    """
    Find if a directory has a module schema config.
    Returns (config_path, errors) where errors is None or list of errors for duplicate modules.
    """
    module_configs = []

    for config_info in configs:
        config_path = Path(config_info['path'])
        if config_path.parent == module_dir:
            if config_info['content'] and config_info['content'].get('schema') == 'module':
                module_configs.append(config_info['path'])

    if len(module_configs) == 0:
        return (None, None)
    elif len(module_configs) == 1:
        return (module_configs[0], None)
    else:
        # Multiple module configs - error
        module_name = str(module_dir) if str(module_dir) != '.' else ''
        return (module_configs[0], [f"duplicate 'module' schema in '{module_name}'"])


def validate_all(configs: List[Dict[str, Any]], base_dir: Path) -> Tuple[bool, List[Dict[str, Any]]]:
    """
    Validate all configs and check for duplicate module schemas and dock configs.

    Returns (all_valid, list_of_file_results).
    """
    # First pass: validate each file individually
    file_results = []
    config_by_path = {}

    for config_info in configs:
        path = config_info['path']

        if config_info['parse_error']:
            file_results.append({
                'path': path,
                'valid': False,
                'errors': [f"parse error: {config_info['parse_error']}"]
            })
            continue

        content = config_info['content']
        config_by_path[path] = config_info

        # Determine schema type and validate accordingly
        schema = content.get('schema')
        if schema == 'module':
            config_path = Path(path)
            module_dir = config_path.parent
            is_valid, errors = validate_module(content, module_dir)
        elif schema == 'preferences':
            is_valid, errors = validate_preferences(content)
        elif schema == 'dock':
            is_valid, errors = validate_dock(content)
        elif schema == 'environments':
            is_valid, errors = validate_environments(content)
        else:
            # Unknown schema - validate as module (which will catch the unknown schema error)
            config_path = Path(path)
            module_dir = config_path.parent
            is_valid, errors = validate_module(content, module_dir)

        result = {
            'path': path,
            'valid': is_valid
        }
        if not is_valid:
            result['errors'] = errors
        file_results.append(result)

    # Second pass: check for duplicate module schemas per directory
    # Group configs by directory
    dir_to_module_configs = {}
    for config_info in configs:
        if config_info['parse_error'] or not config_info['content']:
            continue

        content = config_info['content']
        if content.get('schema') == 'module':
            config_path = Path(config_info['path'])
            module_dir = config_path.parent
            if module_dir not in dir_to_module_configs:
                dir_to_module_configs[module_dir] = []
            dir_to_module_configs[module_dir].append(config_info['path'])

    # Check for duplicates and add error to file results
    for module_dir, module_config_paths in dir_to_module_configs.items():
        if len(module_config_paths) > 1:
            # Add duplicate error to all these files
            module_name = str(module_dir) if str(module_dir) != '.' else ''
            duplicate_error = f"duplicate 'module' schema in '{module_name}'"

            for file_result in file_results:
                if file_result['path'] in module_config_paths:
                    if 'errors' not in file_result or file_result['errors'] is None:
                        file_result['errors'] = []
                    file_result['errors'].append(duplicate_error)
                    file_result['valid'] = False

    # Third pass: check for multiple dock configs across entire tree
    dock_configs = []
    for config_info in configs:
        if config_info['parse_error'] or not config_info['content']:
            continue

        content = config_info['content']
        if content.get('schema') == 'dock':
            dock_configs.append(config_info['path'])

    # If more than one dock config, add error to all of them
    if len(dock_configs) > 1:
        dock_error = "multiple 'dock' schemas found — only one is allowed"
        for file_result in file_results:
            if file_result['path'] in dock_configs:
                if 'errors' not in file_result or file_result['errors'] is None:
                    file_result['errors'] = []
                file_result['errors'].append(dock_error)
                file_result['valid'] = False

    all_valid = all(f['valid'] for f in file_results)
    return (all_valid, file_results)


def expand_wildcard_action(action: Dict[str, Any], module_dir: Path, base_dir: Path) -> List[Dict[str, Any]]:
    """
    Expand wildcard source to concrete file actions.

    Returns list of expanded actions.
    """
    if action.get('source') != '*':
        return [action]

    destination = action.get('destination', '')
    hidden = action.get('hidden', False)
    elevated = action.get('elevated', False)
    action_type = action['type']

    # Find files in module directory
    module_path = base_dir / module_dir
    if not module_path.exists():
        return []

    matched_files = []
    for item in module_path.iterdir():
        # Exclude directories
        if item.is_dir():
            continue
        # Exclude .yaml, .yml, .json
        if item.suffix in ('.yaml', '.yml', '.json'):
            continue
        # Exclude hidden files
        if item.name.startswith('.'):
            continue

        matched_files.append(item.name)

    # Sort by filename
    matched_files.sort()

    # Generate actions
    expanded = []
    for filename in matched_files:
        new_dest = destination + filename

        # Apply hidden modifier
        if hidden and not filename.startswith('.'):
            new_dest = destination + '.' + filename

        new_action = {
            'type': action_type,
            'source': filename,
            'destination': new_dest,
            'elevated': elevated
        }
        expanded.append(new_action)

    return expanded


def resolve_destination(dest: str, hidden: bool = False) -> str:
    """Resolve ~ in destination to absolute path using $HOME.
    Also apply hidden modifier if specified."""
    if hidden:
        # Extract filename and prefix with . if not already
        filename = os.path.basename(dest)
        dirname = os.path.dirname(dest)
        if not filename.startswith('.'):
            filename = '.' + filename
            if dirname:
                dest = os.path.join(dirname, filename)
            else:
                dest = filename

    if dest.startswith('~/'):
        home = os.environ.get('HOME', '')
        return dest.replace('~', home, 1)
    return dest


def generate_plan(configs: List[Dict[str, Any]],
                  base_dir: Path,
                  os_filter: Optional[str] = None,
                  os_version_filter: Optional[str] = None,
                  module_filter: Optional[List[str]] = None) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """
    Generate a plan from validated configs.

    Returns (success, plan_or_error).
    """
    # Validate all configs first
    all_valid, file_results = validate_all(configs, base_dir)

    if not all_valid:
        return (False, {
            'error': 'validation_failed',
            'details': {
                'valid': False,
                'files': file_results
            }
        })

    # Validate os_version_filter if provided
    if os_version_filter is not None:
        if version_index(os_version_filter) == -1:
            return (False, {
                'error': 'unknown_os_version',
                'details': f"unknown version '{os_version_filter}'"
            })

    # Build a map of directories to their configs
    # A module is defined by having a module.yaml (or any config with schema: module)
    # OR by having a preferences.yaml or dock.yaml

    # First, collect all configs by directory
    dir_to_configs: Dict[Path, Dict[str, Any]] = {}
    dock_config = None  # Track single dock config
    dock_config_path = None

    for config_info in configs:
        if config_info['parse_error'] or not config_info['content']:
            continue

        content = config_info['content']
        config_path = Path(config_info['path'])
        module_dir = config_path.parent

        # Skip root-level configs for module purposes
        if str(module_dir) == '.':
            continue

        if module_dir not in dir_to_configs:
            dir_to_configs[module_dir] = {
                'module': None,
                'preferences': [],
                'dock': None,
                'environments': None,
                'all_configs': []  # Track all configs for dependency merging
            }

        # Track all configs for dependency extraction
        dir_to_configs[module_dir]['all_configs'].append(content)

        schema = content.get('schema')
        if schema == 'module':
            dir_to_configs[module_dir]['module'] = {
                'content': content,
                'path': config_info['path']
            }
        elif schema == 'preferences':
            dir_to_configs[module_dir]['preferences'].append({
                'content': content,
                'path': config_info['path']
            })
        elif schema == 'dock':
            if dock_config is not None:
                # This should have been caught by validation, but double-check
                pass
            dock_config = {
                'content': content,
                'path': config_info['path'],
                'module_dir': module_dir
            }
            dock_config_path = config_info['path']
            dir_to_configs[module_dir]['dock'] = {
                'content': content,
                'path': config_info['path']
            }
        elif schema == 'environments':
            dir_to_configs[module_dir]['environments'] = {
                'content': content,
                'path': config_info['path']
            }

    # Build modules list - a directory is a module if it has module config OR preferences/dock/environments config
    modules_data = []

    for module_dir, configs_data in dir_to_configs.items():
        module_name = str(module_dir)

        # Check if this directory has any configs that make it a module
        has_module = configs_data['module'] is not None
        has_preferences = len(configs_data['preferences']) > 0
        has_dock = configs_data['dock'] is not None
        has_environments = configs_data['environments'] is not None

        if not (has_module or has_preferences or has_dock or has_environments):
            continue

        module_content = configs_data['module']['content'] if configs_data['module'] else None

        # Extract and merge dependencies from all configs in this module
        all_deps = set()
        for cfg in configs_data['all_configs']:
            if 'depends_on' in cfg:
                depends_on = cfg['depends_on']
                if isinstance(depends_on, list):
                    for dep in depends_on:
                        if isinstance(dep, str) and dep:
                            all_deps.add(dep)

        # OS filter - check module's os.name
        if os_filter is not None and module_content is not None:
            module_os = module_content.get('os', {}).get('name')
            if module_os is not None and module_os != os_filter:
                # Module's os.name doesn't match filter
                # Only skip if this module would have no other actions (preferences, environments)
                if not has_preferences and not has_dock and not has_environments:
                    continue
                # If it only has dock and --os is not macos, skip
                if not has_preferences and not has_environments and has_dock and os_filter != 'macos':
                    continue

        # Also apply OS filter to modules without module.yaml
        # For preferences-only or dock-only modules, skip if --os linux and they're macOS-only
        if os_filter is not None and module_content is None:
            # No module config, just preferences/dock/environments
            # Preferences are not OS-specific (only version-specific), so keep them
            # Environments are not OS-specific, so keep them
            # Dock is macOS-only, so skip if --os is not macos
            if has_dock and os_filter != 'macos' and not has_preferences and not has_environments:
                continue

        # Module name filter
        if module_filter is not None:
            if module_name not in module_filter:
                continue

        modules_data.append({
            'name': module_name,
            'module_dir': module_dir,
            'module_content': module_content,
            'preferences': configs_data['preferences'],
            'dock': configs_data['dock'],
            'environments': configs_data['environments'],
            'has_module': has_module,
            'dependencies': sorted(list(all_deps))  # Sorted for determinism
        })

    # Sort modules alphabetically (base order, will be updated by topological sort)
    modules_data.sort(key=lambda m: m['name'])

    # First check file_not_found errors (before dependency validation)
    for module_data in modules_data:
        module_name = module_data['name']
        module_dir = module_data['module_dir']
        module_content = module_data['module_content']

        if module_content:
            config_actions = module_content.get('actions', [])
            for action in config_actions:
                expanded = expand_wildcard_action(action, module_dir, base_dir)
                for exp_action in expanded:
                    source = exp_action['source']
                    if source != '*':
                        source_path = base_dir / module_dir / source
                        if not source_path.exists():
                            return (False, {
                                'error': 'file_not_found',
                                'details': {
                                    'module': f"module '{module_name}'",
                                    'missing_source': f"missing source file '{source}'"
                                }
                            })

    # Validate dependencies and perform topological sort
    module_names = {m['name'] for m in modules_data}

    # Check for missing dependencies
    missing_dep_error = validate_dependencies(modules_data, module_names)
    if missing_dep_error:
        return (False, missing_dep_error)

    # Perform topological sort with alphabetical tie-breaking
    success, sorted_names, cycle = topological_sort(modules_data)
    if not success:
        return (False, {
            'error': 'circular_dependency',
            'cycle': cycle
        })

    # Reorder modules_data according to topological sort
    name_to_module = {m['name']: m for m in modules_data}
    modules_data = [name_to_module[name] for name in sorted_names]

    # Generate plan for each module
    plan_modules = []

    # Collect all link/copy actions for conflict detection
    all_file_actions = []

    for module_data in modules_data:
        module_name = module_data['name']
        module_dir = module_data['module_dir']
        module_content = module_data['module_content']
        preferences_configs = module_data['preferences']
        dock_data = module_data['dock']

        actions = []

        # 1. install_package actions (sorted alphabetically)
        if module_content:
            os_config = module_content.get('os', {})
            package_manager = os_config.get('package_manager')

            if package_manager:
                packages = os_config.get('packages', [])
                for pkg in sorted(packages):
                    actions.append({
                        'type': 'install_package',
                        'manager': package_manager,
                        'package': pkg
                    })

                # 2. install_application actions (sorted alphabetically)
                applications = os_config.get('applications', [])
                for app in sorted(applications):
                    actions.append({
                        'type': 'install_application',
                        'manager': package_manager,
                        'application': app
                    })

        # 3. File actions (link, copy, run) in config order
        if module_content:
            config_actions = module_content.get('actions', [])

            for action in config_actions:
                # Expand wildcards
                expanded = expand_wildcard_action(action, module_dir, base_dir)

                for exp_action in expanded:
                    action_type = exp_action['type']
                    source = exp_action['source']

                    # Resolve source path
                    if source != '*':
                        source_path = base_dir / module_dir / source
                        abs_source = str(source_path.resolve())

                        # Check if file exists (non-wildcard only)
                        if not source_path.exists():
                            return (False, {
                                'error': 'file_not_found',
                                'details': {
                                    'module': f"module '{module_name}'",
                                    'missing_source': f"missing source file '{source}'"
                                }
                            })
                    else:
                        # Wildcard - shouldn't reach here as it's expanded
                        continue

                    # Build action
                    if action_type == 'run':
                        actions.append({
                            'type': 'run',
                            'source': abs_source,
                            'elevated': exp_action.get('elevated', False)
                        })
                    else:
                        # link or copy
                        destination = exp_action.get('destination', '')
                        hidden = exp_action.get('hidden', False)
                        resolved_dest = resolve_destination(destination, hidden)

                        file_action = {
                            'type': action_type,
                            'source': abs_source,
                            'destination': resolved_dest,
                            'elevated': exp_action.get('elevated', False)
                        }
                        actions.append(file_action)

                        # Track for conflict detection
                        all_file_actions.append({
                            'module': module_name,
                            'type': action_type,
                            'source': abs_source,
                            'destination': resolved_dest
                        })

        # 4. set_preference actions in order they appear in preferences list
        for pref_config_data in preferences_configs:
            pref_content = pref_config_data['content']
            preferences_list = pref_content.get('preferences', [])

            for pref in preferences_list:
                # Check enabled (defaults to true)
                if not pref.get('enabled', True):
                    continue

                # Check OS version range if filter is applied
                if os_version_filter is not None:
                    min_version = pref.get('min_version')
                    max_version = pref.get('max_version')
                    if not is_version_in_range(os_version_filter, min_version, max_version):
                        continue

                # Build set_preference action
                pref_action = {
                    'type': 'set_preference',
                    'name': pref['name'],
                    'domain': pref['domain'],
                    'key': pref['key'],
                    'value': pref['value'],
                    'value_type': pref['value_type'],
                    'apply_command': pref['apply_command']
                }

                # Include check_command if present
                if 'check_command' in pref:
                    pref_action['check_command'] = pref['check_command']

                # Include expected_state only if both check_command and expected_state exist
                if 'check_command' in pref and 'expected_state' in pref:
                    pref_action['expected_state'] = pref['expected_state']

                actions.append(pref_action)

        # 5. configure_dock action (at most one per entire plan)
        if dock_data:
            # Check OS filter - dock is macos only
            if os_filter is None or os_filter == 'macos':
                dock_content = dock_data['content']
                actions.append({
                    'type': 'configure_dock',
                    'items': dock_content['items']
                })

        # 6. Environment actions (install_runtime, install_plugin, create_virtual_env)
        environments_data = module_data.get('environments')
        if environments_data:
            env_content = environments_data['content']
            env_actions = generate_environment_actions(env_content)
            actions.extend(env_actions)

        plan_modules.append({
            'name': module_name,
            'actions': actions
        })

    # Check for conflicts
    conflicts = detect_conflicts(all_file_actions)
    if conflicts:
        return (False, {
            'error': 'conflict',
            'conflicts': conflicts
        })

    return (True, {'modules': plan_modules})


def detect_conflicts(file_actions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Detect conflicts where multiple link/copy actions target the same destination.

    Returns list of conflicts, sorted by destination.
    """
    # Group actions by destination
    dest_to_actions: Dict[str, List[Dict[str, Any]]] = {}

    for action in file_actions:
        dest = action['destination']
        if dest not in dest_to_actions:
            dest_to_actions[dest] = []
        dest_to_actions[dest].append(action)

    # Find conflicts (destinations with more than one action)
    conflicts = []
    for dest, actions in dest_to_actions.items():
        if len(actions) > 1:
            # Build sources list
            sources = []
            for action in actions:
                sources.append({
                    'module': action['module'],
                    'type': action['type'],
                    'source': action['source']
                })

            # Sort sources by module, then by source, then by type
            sources.sort(key=lambda s: (s['module'], s['source'], s['type']))

            conflicts.append({
                'destination': dest,
                'sources': sources
            })

    # Sort conflicts by destination
    conflicts.sort(key=lambda c: c['destination'])

    return conflicts


def topological_sort(modules: List[Dict[str, Any]]) -> Tuple[bool, Optional[List[str]], Optional[List[str]]]:
    """
    Perform topological sort on modules based on dependencies.

    Returns (success, sorted_module_names, cycle_or_none).
    If there's a cycle, returns (False, None, cycle_list).
    """
    # Build adjacency list
    graph: Dict[str, List[str]] = {m['name']: [] for m in modules}
    in_degree: Dict[str, int] = {m['name']: 0 for m in modules}

    module_names = {m['name'] for m in modules}

    for module in modules:
        name = module['name']
        deps = module.get('dependencies', [])
        for dep in deps:
            if dep in module_names:
                graph[dep].append(name)
                in_degree[name] += 1

    # Kahn's algorithm with alphabetical tie-breaking
    # Use a sorted list as a priority queue
    queue = sorted([name for name, degree in in_degree.items() if degree == 0])
    result = []

    while queue:
        # Pop the first (alphabetically smallest) node
        current = queue.pop(0)
        result.append(current)

        # Get neighbors and sort them
        neighbors = sorted(graph[current])
        for neighbor in neighbors:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                # Insert in sorted position
                queue.append(neighbor)
                queue.sort()

    # Check for cycle
    if len(result) != len(modules):
        # Find cycle using DFS
        cycle = find_cycle(modules, module_names)
        return (False, None, cycle)

    return (True, result, None)


def find_cycle(modules: List[Dict[str, Any]], module_names: set) -> List[str]:
    """
    Find a cycle in the dependency graph using DFS.

    Returns a list representing the cycle (first and last elements are the same).
    """
    # Build adjacency list
    graph: Dict[str, List[str]] = {}
    for m in modules:
        graph[m['name']] = []

    for module in modules:
        name = module['name']
        deps = module.get('dependencies', [])
        for dep in deps:
            if dep in module_names:
                graph[name].append(dep)

    # DFS to find cycle
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {name: WHITE for name in graph}
    parent = {name: None for name in graph}

    def dfs(node: str, path: List[str]) -> Optional[List[str]]:
        color[node] = GRAY
        path.append(node)

        for neighbor in sorted(graph[node]):
            if color[neighbor] == GRAY:
                # Found cycle - construct the cycle path
                cycle_start = path.index(neighbor)
                cycle = path[cycle_start:] + [neighbor]
                return cycle
            elif color[neighbor] == WHITE:
                result = dfs(neighbor, path)
                if result:
                    return result

        color[node] = BLACK
        path.pop()
        return None

    for node in sorted(graph.keys()):
        if color[node] == WHITE:
            cycle = dfs(node, [])
            if cycle:
                return cycle

    # Should not reach here if there's a cycle
    return []


def validate_dependencies(modules: List[Dict[str, Any]], module_names: set) -> Optional[Dict[str, Any]]:
    """
    Validate that all dependencies exist in the module set.

    Returns None if valid, or an error dict if a dependency is missing.
    """
    for module in modules:
        name = module['name']
        deps = module.get('dependencies', [])
        for dep in deps:
            if dep not in module_names:
                return {
                    'error': 'missing_dependency',
                    'module': name,
                    'missing': dep
                }
    return None


def generate_environment_actions(env_config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Generate environment actions from an environments config.

    Actions are generated in the order:
    1. install_runtime for each version (in config order)
    2. install_plugin for each plugin (in config order)
    3. create_virtual_env for each virtual environment (grouped by plugin, preserving order)
    """
    actions = []

    environments = env_config.get('environments', [])
    for env in environments:
        language = env.get('language')
        versions = env.get('versions', [])
        manager = env.get('manager', {})
        manager_name = manager.get('name')

        # 1. Install runtimes for each version
        for version in versions:
            actions.append({
                'type': 'install_runtime',
                'language': language,
                'version': version,
                'manager': manager_name
            })

        # 2. Install plugins
        plugins = manager.get('plugins', [])
        for plugin in plugins:
            plugin_name = plugin.get('name')
            actions.append({
                'type': 'install_plugin',
                'manager': manager_name,
                'plugin': plugin_name
            })

            # 3. Create virtual environments for this plugin
            virtual_envs = plugin.get('virtual_environments', [])
            for venv in virtual_envs:
                actions.append({
                    'type': 'create_virtual_env',
                    'language': language,
                    'manager': manager_name,
                    'plugin': plugin_name,
                    'version': venv.get('version'),
                    'name': venv.get('name')
                })

    return actions


# ============================================================================
# Manifest and Profile Handling
# ============================================================================

def discover_manifest(base_dir: Path) -> Optional[Dict[str, Any]]:
    """
    Discover and parse manifest.yaml or manifest.yml in the base directory.

    Returns the parsed manifest content or None if not found.
    """
    for filename in ('manifest.yaml', 'manifest.yml'):
        manifest_path = base_dir / filename
        if manifest_path.exists():
            try:
                with open(manifest_path, 'r') as f:
                    content = yaml.safe_load(f)
                    if isinstance(content, dict):
                        return content
            except Exception:
                pass
    return None


def validate_manifest(manifest: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """
    Validate a manifest configuration.

    Returns (is_valid, list_of_errors).
    """
    errors = []

    # Check profiles field
    if 'profiles' not in manifest:
        errors.append("profiles: required")
    elif not isinstance(manifest['profiles'], dict):
        errors.append("profiles: must be an object")
    else:
        profiles = manifest['profiles']
        for profile_name, profile_config in profiles.items():
            if not isinstance(profile_config, dict):
                errors.append(f"profiles.{profile_name}: must be an object")
                continue

            # modules is required for each profile
            if 'modules' not in profile_config:
                errors.append(f"profiles.{profile_name}.modules: required")
            elif not isinstance(profile_config['modules'], list):
                errors.append(f"profiles.{profile_name}.modules: must be a list")
            else:
                for i, mod in enumerate(profile_config['modules']):
                    if not isinstance(mod, str):
                        errors.append(f"profiles.{profile_name}.modules[{i}]: must be a string")
                    elif mod == '':
                        errors.append(f"profiles.{profile_name}.modules[{i}]: must be non-empty")

            # extends is optional
            if 'extends' in profile_config:
                extends = profile_config['extends']
                if not isinstance(extends, list):
                    errors.append(f"profiles.{profile_name}.extends: must be a list")
                else:
                    for i, ext in enumerate(extends):
                        if not isinstance(ext, str):
                            errors.append(f"profiles.{profile_name}.extends[{i}]: must be a string")
                        elif ext == '':
                            errors.append(f"profiles.{profile_name}.extends[{i}]: must be non-empty")

    return (len(errors) == 0, errors)


def resolve_profile_modules(profile_name: str, manifest: Dict[str, Any], visited: Optional[Set[str]] = None) -> Tuple[bool, Optional[List[str]], Optional[List[str]]]:
    """
    Resolve the modules for a profile, including inherited modules from extended profiles.

    Returns (success, modules_list, cycle_or_none).
    """
    if visited is None:
        visited = set()

    profiles = manifest.get('profiles', {})

    if profile_name not in profiles:
        return (False, None, None)

    # Check for circular inheritance
    if profile_name in visited:
        return (False, None, [profile_name])

    visited = visited | {profile_name}

    profile_config = profiles[profile_name]

    # Get direct modules
    direct_modules = profile_config.get('modules', [])

    # Get inherited modules from extended profiles
    inherited_modules = []
    extends = profile_config.get('extends', [])

    for ext_profile in extends:
        success, ext_modules, cycle = resolve_profile_modules(ext_profile, manifest, visited)
        if not success:
            if cycle:
                # Prepend current profile to cycle
                cycle.insert(0, profile_name)
            return (False, None, cycle)
        if ext_modules:
            inherited_modules.extend(ext_modules)

    # Combine: inherited modules first, then direct modules
    # But preserve order and remove duplicates (keep first occurrence)
    all_modules = inherited_modules + direct_modules
    seen = set()
    result = []
    for mod in all_modules:
        if mod not in seen:
            seen.add(mod)
            result.append(mod)

    return (True, result, None)


def find_leaf_profiles(manifest: Dict[str, Any]) -> List[str]:
    """
    Find all leaf profiles in the manifest.
    A leaf profile is one that no other profile extends.

    Returns sorted list of leaf profile names.
    """
    profiles = manifest.get('profiles', {})
    if not profiles:
        return []

    # Find all profiles that are extended by others
    extended_profiles: Set[str] = set()
    for profile_name, profile_config in profiles.items():
        extends = profile_config.get('extends', [])
        if isinstance(extends, list):
            for ext in extends:
                if isinstance(ext, str):
                    extended_profiles.add(ext)

    # Leaf profiles are those not in extended_profiles
    leaf_profiles = [name for name in profiles.keys() if name not in extended_profiles]

    return sorted(leaf_profiles)


def validate_profile_exists(profile_name: str, manifest: Dict[str, Any]) -> bool:
    """Check if a profile exists in the manifest."""
    profiles = manifest.get('profiles', {})
    return profile_name in profiles


def find_circular_inheritance(manifest: Dict[str, Any]) -> Optional[List[str]]:
    """
    Check for circular inheritance in profile definitions.

    Returns cycle list if found, None otherwise.
    """
    profiles = manifest.get('profiles', {})
    if not profiles:
        return None

    # Use DFS to find cycles
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {name: WHITE for name in profiles}

    def dfs(node: str, path: List[str]) -> Optional[List[str]]:
        color[node] = GRAY
        path.append(node)

        extends = profiles.get(node, {}).get('extends', [])
        if isinstance(extends, list):
            for ext in sorted(extends):
                if ext not in profiles:
                    continue
                if color[ext] == GRAY:
                    # Found cycle
                    cycle_start = path.index(ext)
                    return path[cycle_start:] + [ext]
                elif color[ext] == WHITE:
                    result = dfs(ext, path)
                    if result:
                        return result

        color[node] = BLACK
        path.pop()
        return None

    for node in sorted(profiles.keys()):
        if color[node] == WHITE:
            cycle = dfs(node, [])
            if cycle:
                return cycle

    return None


def cmd_validate(directory: str) -> int:
    """Execute validate command."""
    configs = discover_configs(directory)
    base_dir = Path(directory).resolve()

    all_valid, file_results = validate_all(configs, base_dir)

    result = {
        'valid': all_valid,
        'files': file_results
    }

    print(json.dumps(result, indent=2))
    return 0 if all_valid else 1


# ============================================================================
# Build Command
# ============================================================================

def generate_installer_script(
    plan: Dict[str, Any],
    os_name: str,
    package_dir: Path,
    source_files: Dict[str, str]
) -> str:
    """
    Generate the installer bash script.

    Args:
        plan: The generated plan with modules and actions
        os_name: Target OS ('macos' or 'linux')
        package_dir: Path to the package directory (for relative source paths)
        source_files: Dict mapping absolute source paths to relative package paths

    Returns:
        The installer script content as a string.
    """
    lines = [
        '#!/bin/bash',
        '# Auto-generated installer script',
        '',
        '# Get the directory where this script is located',
        'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
        '',
        '# Helper function for elevated commands',
        'run_elevated() {',
        '    if [ "$(id -u)" -eq 0 ]; then',
        '        "$@"',
        '    else',
        '        sudo "$@"',
        '    fi',
        '}',
        '',
        'DRY_RUN=false',
        '',
        '# Parse arguments',
        'while [[ $# -gt 0 ]]; do',
        '    case $1 in',
        '        --dry-run)',
        '            DRY_RUN=true',
        '            shift',
        '            ;;',
        '        *)',
        '            shift',
        '            ;;',
        '    esac',
        'done',
        '',
    ]

    # Collect all actions in plan order
    all_actions = []
    for module in plan.get('modules', []):
        module_actions = module.get('actions', [])
        all_actions.extend(module_actions)

    # Generate dry-run and execution logic for each action
    for action in all_actions:
        action_type = action['type']

        if action_type == 'install_package':
            manager = action['manager']
            package = action['package']
            lines.append(f'# install_package {manager} {package}')
            lines.append('if [ "$DRY_RUN" = true ]; then')
            lines.append(f'    echo "[rig] install_package {manager} {package}"')
            lines.append('else')
            if manager == 'brew':
                lines.append(f'    brew install {package}')
            elif manager == 'apt':
                lines.append(f'    sudo apt-get update && sudo apt-get install -y {package}')
            elif manager == 'yum':
                lines.append(f'    sudo yum install -y {package}')
            lines.append('fi')
            lines.append('')

        elif action_type == 'install_application':
            manager = action['manager']
            application = action['application']
            lines.append(f'# install_application {manager} {application}')
            lines.append('if [ "$DRY_RUN" = true ]; then')
            lines.append(f'    echo "[rig] install_application {manager} {application}"')
            lines.append('else')
            if manager == 'brew':
                lines.append(f'    brew install --cask {application}')
            elif manager == 'apt':
                lines.append(f'    sudo apt-get update && sudo apt-get install -y {application}')
            elif manager == 'yum':
                lines.append(f'    sudo yum install -y {application}')
            lines.append('fi')
            lines.append('')

        elif action_type in ('link', 'copy'):
            abs_source = action['source']
            destination = action['destination']
            elevated = action.get('elevated', False)
            # Get relative source path within package
            rel_source = source_files.get(abs_source, abs_source)
            lines.append(f'# {action_type} {rel_source} {destination}')
            lines.append('if [ "$DRY_RUN" = true ]; then')
            lines.append(f'    echo "[rig] {action_type} {rel_source} {destination}"')
            lines.append('else')
            lines.append('    mkdir -p "$(dirname "{}")"'.format(destination))
            if action_type == 'link':
                if elevated:
                    lines.append(f'    run_elevated ln -sf "$SCRIPT_DIR/{rel_source}" "{destination}"')
                else:
                    lines.append(f'    ln -sf "$SCRIPT_DIR/{rel_source}" "{destination}"')
            else:  # copy
                if elevated:
                    lines.append(f'    run_elevated cp "$SCRIPT_DIR/{rel_source}" "{destination}"')
                else:
                    lines.append(f'    cp "$SCRIPT_DIR/{rel_source}" "{destination}"')
            lines.append('fi')
            lines.append('')

        elif action_type == 'run':
            abs_source = action['source']
            elevated = action.get('elevated', False)
            rel_source = source_files.get(abs_source, abs_source)
            lines.append(f'# run {rel_source}')
            lines.append('if [ "$DRY_RUN" = true ]; then')
            lines.append(f'    echo "[rig] run {rel_source}"')
            lines.append('else')
            if elevated:
                lines.append(f'    run_elevated bash "$SCRIPT_DIR/{rel_source}"')
            else:
                lines.append(f'    bash "$SCRIPT_DIR/{rel_source}"')
            lines.append('fi')
            lines.append('')

        elif action_type == 'set_preference':
            name = action['name']
            domain = action['domain']
            key = action['key']
            value = action['value']
            value_type = action['value_type']
            apply_command = action['apply_command']
            check_command = action.get('check_command')
            expected_state = action.get('expected_state')
            lines.append(f'# set_preference {name}')
            lines.append('if [ "$DRY_RUN" = true ]; then')
            lines.append(f'    echo "[rig] set_preference {name}"')
            lines.append('else')
            if check_command and expected_state is not None:
                lines.append(f'    CURRENT_STATE=$({check_command} 2>/dev/null || echo "")')
                lines.append(f'    if [ "$CURRENT_STATE" = "{expected_state}" ]; then')
                lines.append(f'        echo "Preference already set: {name}"')
                lines.append('    else')
                lines.append(f'        {apply_command}')
                lines.append('    fi')
            else:
                lines.append(f'    {apply_command}')
            lines.append('fi')
            lines.append('')

        elif action_type == 'configure_dock':
            items = action['items']
            n_items = len(items)
            lines.append(f'# configure_dock {n_items} items')
            lines.append('if [ "$DRY_RUN" = true ]; then')
            lines.append(f'    echo "[rig] configure_dock {n_items} items"')
            lines.append('else')
            # macOS Dock configuration using defaults
            lines.append('    # Clear current Dock')
            lines.append('    defaults write com.apple.dock persistent-apps -array')
            lines.append('    for item in {}; do'.format(' '.join(f'"{item}"' for item in items)))
            lines.append('        defaults write com.apple.dock persistent-apps -array-add "<dict><key>tile-data</key><dict><key>file-label</key><string>$item</string></dict></dict>"')
            lines.append('    done')
            lines.append('    killall Dock')
            lines.append('fi')
            lines.append('')

        elif action_type == 'install_runtime':
            manager = action['manager']
            language = action['language']
            version = action['version']
            lines.append(f'# install_runtime {manager} {language} {version}')
            lines.append('if [ "$DRY_RUN" = true ]; then')
            lines.append(f'    echo "[rig] install_runtime {manager} {language} {version}"')
            lines.append('else')
            if manager == 'pyenv':
                lines.append(f'    pyenv install -s {version}')
            elif manager == 'asdf':
                lines.append(f'    asdf install {language} {version}')
            else:
                # Generic manager - user must handle installation
                lines.append(f'    echo "TODO: install {language} {version} using {manager}"')
            lines.append('fi')
            lines.append('')

        elif action_type == 'install_plugin':
            manager = action['manager']
            plugin = action['plugin']
            lines.append(f'# install_plugin {manager} {plugin}')
            lines.append('if [ "$DRY_RUN" = true ]; then')
            lines.append(f'    echo "[rig] install_plugin {manager} {plugin}"')
            lines.append('else')
            if manager == 'pyenv':
                lines.append(f'    git clone https://github.com/pyenv/{plugin}.git "$(pyenv root)/plugins/{plugin}" 2>/dev/null || true')
            elif manager == 'asdf':
                lines.append(f'    asdf plugin add {plugin}')
            else:
                # Generic manager - user must handle plugin installation
                lines.append(f'    echo "TODO: install plugin {plugin} using {manager}"')
            lines.append('fi')
            lines.append('')

        elif action_type == 'create_virtual_env':
            manager = action['manager']
            plugin = action['plugin']
            name = action['name']
            version = action.get('version', '')
            lines.append(f'# create_virtual_env {manager} {plugin} {name}')
            lines.append('if [ "$DRY_RUN" = true ]; then')
            lines.append(f'    echo "[rig] create_virtual_env {manager} {plugin} {name}"')
            lines.append('else')
            if manager == 'pyenv':
                lines.append(f'    pyenv virtualenv {version} {name}')
            elif manager == 'asdf':
                lines.append(f'    asdf shell {version}')
                lines.append(f'    asdf venv {name}')
            else:
                # Generic manager - user must handle virtual environment creation
                lines.append(f'    echo "TODO: create virtual env {name} using {manager}/{plugin}"')
            lines.append('fi')
            lines.append('')

    return '\n'.join(lines)


def collect_source_files(plan: Dict[str, Any], base_dir: Path) -> Dict[str, Tuple[str, str]]:
    """
    Collect all source files referenced by link, copy, and run actions.

    Returns a dict mapping:
        absolute_source_path -> (module_name, relative_source_path)

    The relative_source_path is the path relative to the module directory.
    """
    source_files = {}

    for module in plan.get('modules', []):
        module_name = module['name']
        module_actions = module.get('actions', [])

        for action in module_actions:
            action_type = action['type']
            if action_type in ('link', 'copy', 'run'):
                abs_source = action['source']
                if abs_source not in source_files:
                    # Compute relative path within module
                    # Source is already absolute, find the module dir
                    module_path = base_dir / module_name
                    try:
                        rel_to_module = str(Path(abs_source).relative_to(module_path))
                    except ValueError:
                        # Source is not under module dir (shouldn't happen but handle it)
                        rel_to_module = Path(abs_source).name

                    source_files[abs_source] = (module_name, rel_to_module)

    return source_files


def create_package(
    plan: Dict[str, Any],
    os_name: str,
    profile_name: str,
    output_dir: Path,
    base_dir: Path,
    create_archive: bool = False
) -> Tuple[bool, Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    Create a package directory with installer and source files.

    Returns (success, package_info, error).
    """
    # Create package directory name
    package_name = f"{profile_name}-package"
    package_path = output_dir / package_name

    # Create output directory if it doesn't exist
    output_dir.mkdir(parents=True, exist_ok=True)

    # Remove existing package directory if it exists
    if package_path.exists():
        shutil.rmtree(package_path)

    # Create fresh package directory
    package_path.mkdir(parents=True)

    # Collect source files
    source_files = collect_source_files(plan, base_dir)

    # Map from absolute source to relative path in package
    package_source_map = {}

    # Copy source files to package, preserving module directory structure
    for abs_source, (module_name, rel_source) in source_files.items():
        source_path = Path(abs_source)

        if not source_path.exists():
            # This should have been caught during plan generation, but check anyway
            return (False, None, {
                'error': 'file_not_found',
                'details': {
                    'module': f"module '{module_name}'",
                    'missing_source': f"missing source file '{abs_source}'"
                }
            })

        # Destination within package: <package>/<module>/<rel_source>
        dest_in_package = package_path / module_name / rel_source
        dest_in_package.parent.mkdir(parents=True, exist_ok=True)

        # Copy the file
        shutil.copy2(source_path, dest_in_package)

        # Record the relative path for installer script
        package_rel_path = f"{module_name}/{rel_source}"
        package_source_map[abs_source] = package_rel_path

    # Generate installer script
    installer_script = generate_installer_script(plan, os_name, package_path, package_source_map)

    # Write installer script
    installer_path = package_path / 'install'
    with open(installer_path, 'w') as f:
        f.write(installer_script)

    # Make installer executable
    installer_path.chmod(installer_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # Build package info
    module_names = [m['name'] for m in plan.get('modules', [])]
    package_info = {
        'profile': profile_name,
        'os': os_name,
        'directory': str(package_path),
        'modules': module_names
    }

    # Create archive if requested
    if create_archive:
        archive_path = output_dir / f"{package_name}.tar.gz"
        with tarfile.open(archive_path, 'w:gz') as tar:
            tar.add(package_path, arcname=package_name)
        package_info['archive'] = str(archive_path)

    return (True, package_info, None)


def generate_plan_for_profile(
    configs: List[Dict[str, Any]],
    base_dir: Path,
    profile_name: str,
    module_names: List[str],
    os_filter: str,
    os_version_filter: Optional[str] = None
) -> Tuple[bool, Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    Generate a plan for a specific profile's module list.

    Returns (success, plan_or_none, error_or_none).
    """
    # We need to modify generate_plan to accept a module_filter
    # and return modules in the order specified by the profile
    # For now, use the module_filter parameter

    success, result = generate_plan(configs, base_dir, os_filter, os_version_filter, module_names)

    if not success:
        return (False, None, result)

    # The plan modules are in topological order, but we need them
    # to respect the profile's order as much as possible while
    # still respecting dependencies.
    # For now, let's reorder based on profile order where possible.

    return (True, result, None)


def cmd_build(
    directory: str,
    os_name: str,
    os_version: Optional[str] = None,
    profile_name: Optional[str] = None,
    output_dir: Optional[str] = None,
    release: bool = False
) -> int:
    """Execute build command."""
    base_dir = Path(directory).resolve()

    if not base_dir.exists() or not base_dir.is_dir():
        error = {
            'error': 'no_configs',
            'details': f"directory '{directory}' not found"
        }
        print(json.dumps(error), file=sys.stderr)
        return 1

    # Validate OS
    if os_name not in ('macos', 'linux'):
        error = {
            'error': 'invalid_os',
            'details': f"unsupported OS '{os_name}'"
        }
        print(json.dumps(error), file=sys.stderr)
        return 1

    # Validate OS version if provided
    if os_version and version_index(os_version) == -1:
        error = {
            'error': 'unknown_os_version',
            'details': f"unknown version '{os_version}'"
        }
        print(json.dumps(error), file=sys.stderr)
        return 1

    # Discover configs
    configs = discover_configs(directory)

    if len(configs) == 0:
        error = {
            'error': 'no_configs',
            'details': 'no rig config files discovered'
        }
        print(json.dumps(error), file=sys.stderr)
        return 1

    # Validate all configs first
    all_valid, file_results = validate_all(configs, base_dir)
    if not all_valid:
        error = {
            'error': 'validation_failed',
            'details': {
                'valid': False,
                'files': file_results
            }
        }
        print(json.dumps(error), file=sys.stderr)
        return 1

    # Discover manifest
    manifest = discover_manifest(base_dir)

    # Determine output directory
    if output_dir:
        out_path = Path(output_dir).resolve()
    else:
        out_path = base_dir / '.rig' / 'build'

    # Determine which profiles to build
    profiles_to_build = []

    if manifest:
        # Validate manifest
        manifest_valid, manifest_errors = validate_manifest(manifest)
        if not manifest_valid:
            error = {
                'error': 'invalid_manifest',
                'details': manifest_errors
            }
            print(json.dumps(error), file=sys.stderr)
            return 1

        # Check for circular inheritance
        cycle = find_circular_inheritance(manifest)
        if cycle:
            error = {
                'error': 'circular_inheritance',
                'cycle': cycle
            }
            print(json.dumps(error), file=sys.stderr)
            return 1

        if profile_name:
            # Single profile specified
            if not validate_profile_exists(profile_name, manifest):
                error = {
                    'error': 'unknown_profile',
                    'details': f"unknown profile '{profile_name}'"
                }
                print(json.dumps(error), file=sys.stderr)
                return 1

            # Resolve modules for this profile
            success, modules, cycle = resolve_profile_modules(profile_name, manifest)
            if not success:
                if cycle:
                    error = {
                        'error': 'circular_inheritance',
                        'cycle': cycle
                    }
                else:
                    error = {
                        'error': 'invalid_profile',
                        'details': f"could not resolve profile '{profile_name}'"
                    }
                print(json.dumps(error), file=sys.stderr)
                return 1

            profiles_to_build = [(profile_name, modules)]
        else:
            # Build all leaf profiles
            leaf_profiles = find_leaf_profiles(manifest)
            for leaf_name in leaf_profiles:
                success, modules, cycle = resolve_profile_modules(leaf_name, manifest)
                if not success:
                    if cycle:
                        error = {
                            'error': 'circular_inheritance',
                            'cycle': cycle
                        }
                    else:
                        error = {
                            'error': 'invalid_profile',
                            'details': f"could not resolve profile '{leaf_name}'"
                        }
                    print(json.dumps(error), file=sys.stderr)
                    return 1

                if modules is not None:
                    profiles_to_build.append((leaf_name, modules))
    else:
        # No manifest - build one package with "default" profile
        # Use all discovered modules
        if profile_name:
            error = {
                'error': 'unknown_profile',
                'details': f"unknown profile '{profile_name}'"
            }
            print(json.dumps(error), file=sys.stderr)
            return 1

        # Get all module names from discovered configs
        module_names = []
        for config_info in configs:
            if config_info['parse_error'] or not config_info['content']:
                continue
            config_path = Path(config_info['path'])
            if str(config_path.parent) != '.':
                module_names.append(str(config_path.parent))

        # Remove duplicates while preserving order
        seen = set()
        unique_modules = []
        for mod in module_names:
            if mod not in seen:
                seen.add(mod)
                unique_modules.append(mod)
        module_names = unique_modules

        if module_names:
            profiles_to_build = [('default', module_names)]
        else:
            # No modules found
            profiles_to_build = []

    # Build packages for each profile
    packages = []

    for prof_name, prof_modules in profiles_to_build:
        # Generate plan for this profile
        success, plan, error = generate_plan_for_profile(
            configs, base_dir, prof_name, prof_modules, os_name, os_version
        )

        if not success:
            print(json.dumps(error), file=sys.stderr)
            return 1

        # Create package
        success, pkg_info, error = create_package(
            plan, os_name, prof_name, out_path, base_dir, release
        )

        if not success:
            print(json.dumps(error), file=sys.stderr)
            return 1

        packages.append(pkg_info)

    # Sort packages by profile name (alphabetically)
    packages.sort(key=lambda p: p['profile'])

    # Output result
    result = {'packages': packages}
    print(json.dumps(result, indent=2))

    return 0


def cmd_plan(directory: str, os_filter: Optional[str] = None, os_version_filter: Optional[str] = None, module_filter: Optional[List[str]] = None) -> int:
    """Execute plan command."""
    configs = discover_configs(directory)
    base_dir = Path(directory).resolve()

    # Check if no configs were discovered
    if len(configs) == 0:
        error = {
            'error': 'no_configs',
            'details': 'no rig config files discovered'
        }
        print(json.dumps(error), file=sys.stderr)
        return 1

    success, result = generate_plan(configs, base_dir, os_filter, os_version_filter, module_filter)

    if not success:
        print(json.dumps(result), file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2))
    return 0


def main():
    if len(sys.argv) < 2:
        print("Usage: rig.py <command> [options]", file=sys.stderr)
        return 1

    command = sys.argv[1]

    if command == 'validate':
        if len(sys.argv) < 3:
            print("Usage: rig.py validate <dir>", file=sys.stderr)
            return 1

        directory = sys.argv[2]
        if not os.path.isdir(directory):
            error = {
                'error': 'no_configs',
                'details': f"directory '{directory}' not found"
            }
            print(json.dumps(error), file=sys.stderr)
            return 1

        return cmd_validate(directory)

    elif command == 'plan':
        if len(sys.argv) < 3:
            print("Usage: rig.py plan <dir> [--os <name>] [--os-version <version>] [--module <name> ...]", file=sys.stderr)
            return 1

        directory = sys.argv[2]
        if not os.path.isdir(directory):
            error = {
                'error': 'no_configs',
                'details': f"directory '{directory}' not found"
            }
            print(json.dumps(error), file=sys.stderr)
            return 1

        # Parse optional arguments
        os_filter = None
        os_version_filter = None
        module_filter = None

        i = 3
        while i < len(sys.argv):
            if sys.argv[i] == '--os':
                if i + 1 < len(sys.argv):
                    os_filter = sys.argv[i + 1]
                    i += 2
                else:
                    print("Error: --os requires a value", file=sys.stderr)
                    return 1
            elif sys.argv[i] == '--os-version':
                if i + 1 < len(sys.argv):
                    os_version_filter = sys.argv[i + 1]
                    i += 2
                else:
                    print("Error: --os-version requires a value", file=sys.stderr)
                    return 1
            elif sys.argv[i] == '--module':
                module_filter = []
                i += 1
                while i < len(sys.argv) and not sys.argv[i].startswith('--'):
                    module_filter.append(sys.argv[i])
                    i += 1
            else:
                i += 1

        return cmd_plan(directory, os_filter, os_version_filter, module_filter)

    elif command == 'build':
        if len(sys.argv) < 3:
            print("Usage: rig.py build <dir> --os <name> [--os-version <version>] [--profile <name>] [--output <dir>] [--release]", file=sys.stderr)
            return 1

        # --os is required for build
        os_name = None
        os_version = None
        profile_name = None
        output_dir = None
        release = False

        directory = sys.argv[2]

        # Parse arguments
        i = 3
        while i < len(sys.argv):
            if sys.argv[i] == '--os':
                if i + 1 < len(sys.argv):
                    os_name = sys.argv[i + 1]
                    i += 2
                else:
                    error = {
                        'error': 'missing_flag',
                        'details': '--os requires a value'
                    }
                    print(json.dumps(error), file=sys.stderr)
                    return 1
            elif sys.argv[i] == '--os-version':
                if i + 1 < len(sys.argv):
                    os_version = sys.argv[i + 1]
                    i += 2
                else:
                    error = {
                        'error': 'missing_flag',
                        'details': '--os-version requires a value'
                    }
                    print(json.dumps(error), file=sys.stderr)
                    return 1
            elif sys.argv[i] == '--profile':
                if i + 1 < len(sys.argv):
                    profile_name = sys.argv[i + 1]
                    i += 2
                else:
                    error = {
                        'error': 'missing_flag',
                        'details': '--profile requires a value'
                    }
                    print(json.dumps(error), file=sys.stderr)
                    return 1
            elif sys.argv[i] == '--output':
                if i + 1 < len(sys.argv):
                    output_dir = sys.argv[i + 1]
                    i += 2
                else:
                    error = {
                        'error': 'missing_flag',
                        'details': '--output requires a value'
                    }
                    print(json.dumps(error), file=sys.stderr)
                    return 1
            elif sys.argv[i] == '--release':
                release = True
                i += 1
            else:
                i += 1

        # Check required --os flag
        if os_name is None:
            error = {
                'error': 'missing_flag',
                'details': '--os is required for build'
            }
            print(json.dumps(error), file=sys.stderr)
            return 1

        return cmd_build(directory, os_name, os_version, profile_name, output_dir, release)

    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())