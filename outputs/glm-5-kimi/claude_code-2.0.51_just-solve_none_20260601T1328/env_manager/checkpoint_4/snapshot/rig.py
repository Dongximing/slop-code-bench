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
    try:
        return OS_VERSIONS.index(version)
    except ValueError:
        return -1


def is_version_in_range(version: str, min_version: Optional[str], max_version: Optional[str]) -> bool:
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
    config_files = []

    for root, dirs, files in os.walk(base_dir):
        if '.git' in dirs:
            dirs.remove('.git')

        for file in files:
            if file.endswith(('.yaml', '.yml', '.json')):
                config_files.append(Path(root) / file)

    return sorted(config_files)


def is_rig_config(data: Dict[str, Any]) -> bool:
    return 'version' in data


def validate_string_field(value: Any, field_name: str) -> Optional[str]:
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

    if 'version' not in data:
        errors.append("version: required")
    else:
        version = data['version']
        if not isinstance(version, str):
            errors.append("version: must be a string")
        elif version != "1":
            errors.append(f"unsupported version '{version}'")

    if 'schema' not in data:
        errors.append("schema: required")
    else:
        schema = data['schema']
        if not isinstance(schema, str):
            errors.append("schema: must be a string")

    if 'preferences' not in data:
        errors.append("preferences: required")
    elif not isinstance(data['preferences'], list):
        errors.append("preferences: must be a list")
    else:
        for i, pref in enumerate(data['preferences']):
            if not isinstance(pref, dict):
                errors.append(f"preferences[{i}]: must be an object")
                continue

            for field in ['name', 'domain', 'key', 'value', 'value_type', 'apply_command']:
                if field not in pref:
                    errors.append(f"preferences[{i}].{field}: required")

            if 'value_type' in pref:
                value_type = pref['value_type']
                if not isinstance(value_type, str):
                    errors.append(f"preferences[{i}].value_type: must be a string")
                elif value_type not in ('bool', 'int', 'string', 'float'):
                    errors.append(f"preferences[{i}].value_type: must be 'bool', 'int', 'string', or 'float', got '{value_type}'")

            if 'min_version' in pref:
                min_ver = pref['min_version']
                if not isinstance(min_ver, str):
                    errors.append(f"preferences[{i}].min_version: must be a string")
                elif version_index(min_ver) == -1:
                    errors.append(f"preferences[{i}].min_version: unknown version '{min_ver}'")

            if 'max_version' in pref:
                max_ver = pref['max_version']
                if not isinstance(max_ver, str):
                    errors.append(f"preferences[{i}].max_version: must be a string")
                elif version_index(max_ver) == -1:
                    errors.append(f"preferences[{i}].max_version: unknown version '{max_ver}'")

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

    if 'version' not in data:
        errors.append("version: required")
    else:
        version = data['version']
        if not isinstance(version, str):
            errors.append("version: must be a string")
        elif version != "1":
            errors.append(f"unsupported version '{version}'")

    if 'schema' not in data:
        errors.append("schema: required")
    else:
        schema = data['schema']
        if not isinstance(schema, str):
            errors.append("schema: must be a string")

    if 'os' not in data:
        errors.append("dock: 'os' is required and must be 'macos'")
    else:
        os_val = data['os']
        if not isinstance(os_val, str):
            errors.append("dock: 'os' is required and must be 'macos'")
        elif os_val != 'macos':
            errors.append("dock: 'os' is required and must be 'macos'")

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


def validate_environments_config(data: Dict[str, Any]) -> List[str]:
    """Validate an environments config according to the schema.

    Returns a list of error messages.
    """
    errors = []

    if 'version' not in data:
        errors.append("version: required")
    else:
        version = data['version']
        if not isinstance(version, str):
            errors.append("version: must be a string")
        elif version != "1":
            errors.append(f"unsupported version '{version}'")

    if 'schema' not in data:
        errors.append("schema: required")
    else:
        schema = data['schema']
        if not isinstance(schema, str):
            errors.append("schema: must be a string")

    if 'environments' not in data:
        errors.append("environments: required")
    elif not isinstance(data['environments'], list):
        errors.append("environments: must be a list")
    elif len(data['environments']) == 0:
        errors.append("environments: must be non-empty")
    else:
        for i, env in enumerate(data['environments']):
            if not isinstance(env, dict):
                errors.append(f"environments[{i}]: must be an object")
                continue

            if 'language' not in env:
                errors.append(f"environments[{i}].language: required")
            elif not isinstance(env['language'], str):
                errors.append(f"environments[{i}].language: must be a string")
            elif not env['language']:
                errors.append(f"environments[{i}].language: must be non-empty")

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
                    elif not ver:
                        errors.append(f"environments[{i}].versions[{j}]: must be non-empty")

            if 'manager' not in env:
                errors.append(f"environments[{i}].manager: required")
            elif not isinstance(env['manager'], dict):
                errors.append(f"environments[{i}].manager: must be an object")
            else:
                manager = env['manager']

                if 'name' not in manager:
                    errors.append(f"environments[{i}].manager.name: required")
                elif not isinstance(manager['name'], str):
                    errors.append(f"environments[{i}].manager.name: must be a string")
                elif not manager['name']:
                    errors.append(f"environments[{i}].manager.name: must be non-empty")

                if 'plugins' in manager:
                    if not isinstance(manager['plugins'], list):
                        errors.append(f"environments[{i}].manager.plugins: must be a list")
                    else:
                        for k, plugin in enumerate(manager['plugins']):
                            if not isinstance(plugin, dict):
                                errors.append(f"environments[{i}].manager.plugins[{k}]: must be an object")
                                continue

                            if 'name' not in plugin:
                                errors.append(f"environments[{i}].manager.plugins[{k}].name: required")
                            elif not isinstance(plugin['name'], str):
                                errors.append(f"environments[{i}].manager.plugins[{k}].name: must be a string")
                            elif not plugin['name']:
                                errors.append(f"environments[{i}].manager.plugins[{k}].name: must be non-empty")

                            if 'virtual_environments' in plugin:
                                if not isinstance(plugin['virtual_environments'], list):
                                    errors.append(f"environments[{i}].manager.plugins[{k}].virtual_environments: must be a list")
                                else:
                                    for m, venv in enumerate(plugin['virtual_environments']):
                                        if not isinstance(venv, dict):
                                            errors.append(f"environments[{i}].manager.plugins[{k}].virtual_environments[{m}]: must be an object")
                                            continue

                                        if 'version' not in venv:
                                            errors.append(f"environments[{i}].manager.plugins[{k}].virtual_environments[{m}].version: required")
                                        elif not isinstance(venv['version'], str):
                                            errors.append(f"environments[{i}].manager.plugins[{k}].virtual_environments[{m}].version: must be a string")
                                        elif 'versions' in env and isinstance(env['versions'], list):
                                            if venv['version'] not in env['versions']:
                                                errors.append(f"environments[{i}].manager.plugins[{k}].virtual_environments[{m}].version: '{venv['version']}' is not in the environment's versions list")

                                        if 'name' not in venv:
                                            errors.append(f"environments[{i}].manager.plugins[{k}].virtual_environments[{m}].name: required")
                                        elif not isinstance(venv['name'], str):
                                            errors.append(f"environments[{i}].manager.plugins[{k}].virtual_environments[{m}].name: must be a string")
                                        elif not venv['name']:
                                            errors.append(f"environments[{i}].manager.plugins[{k}].virtual_environments[{m}].name: must be non-empty")

    return errors


def validate_depends_on(data: Dict[str, Any]) -> List[str]:
    """Validate depends_on field if present.

    Returns a list of error messages.
    """
    errors = []

    if 'depends_on' in data:
        depends_on = data['depends_on']
        if not isinstance(depends_on, list):
            errors.append("depends_on: must be a list")
        else:
            for i, dep in enumerate(depends_on):
                if not isinstance(dep, str):
                    errors.append(f"depends_on[{i}]: must be a string")
                elif not dep:
                    errors.append(f"depends_on[{i}]: must be non-empty")

    return errors


def validate_manifest_config(data: Dict[str, Any]) -> List[str]:
    """Validate a manifest config according to the schema.

    Returns a list of error messages.
    """
    errors = []

    if 'version' not in data:
        errors.append("version: required")
    else:
        version = data['version']
        if not isinstance(version, str):
            errors.append("version: must be a string")
        elif version != "1":
            errors.append(f"unsupported version '{version}'")

    if 'schema' not in data:
        errors.append("schema: required")
    else:
        schema = data['schema']
        if not isinstance(schema, str):
            errors.append("schema: must be a string")
        elif schema != "manifest":
            errors.append(f"expected schema 'manifest', got '{schema}'")

    if 'profiles' not in data:
        errors.append("profiles: required")
    elif not isinstance(data['profiles'], dict):
        errors.append("profiles: must be an object")
    elif len(data['profiles']) == 0:
        errors.append("profiles: must be non-empty")
    else:
        profiles = data['profiles']
        for profile_name, profile_def in profiles.items():
            if not isinstance(profile_def, dict):
                errors.append(f"profiles.{profile_name}: must be an object")
                continue

            if 'modules' not in profile_def:
                errors.append(f"profiles.{profile_name}.modules: required")
            elif not isinstance(profile_def['modules'], list):
                errors.append(f"profiles.{profile_name}.modules: must be a list")
            elif len(profile_def['modules']) == 0:
                errors.append(f"profiles.{profile_name}.modules: must be non-empty")
            else:
                for i, mod in enumerate(profile_def['modules']):
                    if not isinstance(mod, str):
                        errors.append(f"profiles.{profile_name}.modules[{i}]: must be a string")
                    elif not mod:
                        errors.append(f"profiles.{profile_name}.modules[{i}]: must be non-empty")

            if 'extends' in profile_def:
                extends = profile_def['extends']
                if not isinstance(extends, str):
                    errors.append(f"profiles.{profile_name}.extends: must be a string")
                elif extends not in profiles:
                    errors.append(f"profiles.{profile_name}.extends: unknown profile '{extends}'")

    return errors


def detect_circular_inheritance(profiles: Dict[str, Any]) -> Optional[List[str]]:
    """Detect circular inheritance in profile definitions.

    Returns the cycle if found, None otherwise.
    """
    visited: set = set()
    rec_stack: set = set()

    def find_cycle(profile: str, path: List[str]) -> Optional[List[str]]:
        visited.add(profile)
        rec_stack.add(profile)
        path.append(profile)

        profile_def = profiles.get(profile, {})
        extends = profile_def.get('extends')

        if extends and extends in profiles:
            if extends not in visited:
                cycle = find_cycle(extends, path)
                if cycle:
                    return cycle
            elif extends in rec_stack:
                cycle_start = path.index(extends)
                return path[cycle_start:] + [extends]

        path.pop()
        rec_stack.remove(profile)
        return None

    for profile_name in profiles:
        if profile_name not in visited:
            cycle = find_cycle(profile_name, [])
            if cycle:
                return cycle

    return None


def resolve_profile(profile_name: str, profiles: Dict[str, Any]) -> List[str]:
    """Resolve a profile to its full module list, including inherited modules.

    Returns the list of module names, deduplicated, in inheritance order.
    """
    def get_modules(profile: str, visited: set) -> List[str]:
        if profile in visited:
            return []
        visited.add(profile)

        profile_def = profiles.get(profile, {})
        modules = []

        # First get inherited modules
        extends = profile_def.get('extends')
        if extends and extends in profiles:
            modules.extend(get_modules(extends, visited))

        # Then add own modules
        own_modules = profile_def.get('modules', [])
        modules.extend(own_modules)

        return modules

    # Get all modules in order, then deduplicate while preserving order
    all_modules = get_modules(profile_name, set())
    seen = set()
    result = []
    for mod in all_modules:
        if mod not in seen:
            seen.add(mod)
            result.append(mod)

    return result


def discover_manifest(base_dir: Path) -> Optional[Tuple[Path, Dict[str, Any]]]:
    """Discover manifest config in the root directory.

    Returns tuple of (file_path, data) if found, None otherwise.
    """
    config_files = discover_config_files(base_dir)
    manifest_configs = []

    for file_path in config_files:
        # Check if file is in root directory (no subdirectory)
        rel_path = file_path.relative_to(base_dir)
        parts = rel_path.parts

        # Manifest must be directly in root (only filename, no subdirectories)
        if len(parts) != 1:
            continue

        data, parse_error = parse_config_file(file_path)
        if parse_error:
            continue

        if is_rig_config(data) and data.get('schema') == 'manifest':
            manifest_configs.append((file_path, data))

    if len(manifest_configs) == 0:
        return None

    if len(manifest_configs) > 1:
        # Multiple manifests - will be handled as error in validation
        return manifest_configs[0]

    return manifest_configs[0]


def validate_manifest(base_dir: Path) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Validate manifest and return manifest data or error.

    Returns:
        Tuple of (manifest_data, error). On success, manifest_data is returned.
        On failure, manifest_data is None and error is returned.
    """
    config_files = discover_config_files(base_dir)
    manifest_configs = []
    manifest_in_module = None

    for file_path in config_files:
        rel_path = file_path.relative_to(base_dir)
        parts = rel_path.parts

        data, parse_error = parse_config_file(file_path)
        if parse_error:
            continue

        if is_rig_config(data) and data.get('schema') == 'manifest':
            # Check if manifest is in root (only 1 part = the filename)
            if len(parts) == 1:
                manifest_configs.append((file_path, data))
            else:
                # Manifest is inside a module directory
                module_name = parts[0]
                manifest_in_module = module_name

    # Check for manifest in module directory first
    if manifest_in_module:
        return None, {
            "error": "validation_failed",
            "details": {"valid": False, "files": [{"path": manifest_in_module, "valid": False, "errors": [f"manifest schema must be in the root directory, found in '{manifest_in_module}'"]}]}}
    if len(manifest_configs) > 1:
        return None, {
            "error": "validation_failed",
            "details": {"valid": False, "files": [{"path": str(m[0].relative_to(base_dir)), "valid": False, "errors": ["multiple 'manifest' schemas found — only one is allowed"]} for m in manifest_configs]}}

    if len(manifest_configs) == 0:
        return None, None  # No manifest is OK

    file_path, data = manifest_configs[0]
    errors = validate_manifest_config(data)

    if errors:
        return None, {
            "error": "validation_failed",
            "details": {"valid": False, "files": [{"path": str(file_path.relative_to(base_dir)), "valid": False, "errors": errors}]}}
    # Check for circular inheritance
    profiles = data.get('profiles', {})
    cycle = detect_circular_inheritance(profiles)
    if cycle:
        return None, {
            "error": "circular_inheritance",
            "cycle": cycle}

    return data, None


def validate_module_config(data: Dict[str, Any], file_path: Path, base_dir: Path, module_dirs: Dict[str, List[Path]]) -> List[str]:
    """Validate a module config according to the schema.

    Returns a list of error messages.
    """
    errors = []

    if 'version' not in data:
        errors.append("version: required")
    else:
        version = data['version']
        if not isinstance(version, str):
            errors.append("version: must be a string")
        elif version != "1":
            errors.append(f"unsupported version '{version}'")

    if 'schema' not in data:
        errors.append("schema: required")
    else:
        schema = data['schema']
        if not isinstance(schema, str):
            errors.append("schema: must be a string")
        elif schema not in ('module', 'preferences', 'dock', 'environments', 'manifest'):
            errors.append(f"unknown schema '{schema}'")

    if 'schema' in data and data.get('schema') != "module":
        return errors

    os_config = data.get('os')
    if os_config is not None:
        if not isinstance(os_config, dict):
            errors.append("os: must be an object")
            os_config = None

    if os_config is not None:
        if 'name' not in os_config:
            errors.append("os.name: required")
        else:
            os_name = os_config['name']
            if not isinstance(os_name, str):
                errors.append("os.name: must be a string")
            elif os_name not in ('macos', 'linux'):
                errors.append(f"os.name: must be 'macos' or 'linux', got '{os_name}'")

        has_packages = 'packages' in os_config
        has_applications = 'applications' in os_config

        if has_packages or has_applications:
            if 'package_manager' not in os_config:
                errors.append("os.package_manager: required when packages or applications present")
            else:
                pm = os_config['package_manager']
                if not isinstance(pm, str):
                    errors.append("os.package_manager: must be a string")
                elif pm not in ('brew', 'apt', 'yum'):
                    errors.append(f"os.package_manager: must be 'brew', 'apt', or 'yum', got '{pm}'")

        if 'packages' in os_config:
            valid, err = validate_list_of_strings(os_config['packages'], 'os.packages')
            if not valid:
                errors.append(err)

        if 'applications' in os_config:
            valid, err = validate_list_of_strings(os_config['applications'], 'os.applications')
            if not valid:
                errors.append(err)

    actions = data.get('actions')
    if actions is not None:
        if not isinstance(actions, list):
            errors.append("actions: must be a list")
        else:
            for i, action in enumerate(actions):
                if not isinstance(action, dict):
                    errors.append(f"actions[{i}]: must be an object")
                    continue

                if 'type' not in action:
                    errors.append(f"actions[{i}].type: required")
                else:
                    action_type = action['type']
                    if not isinstance(action_type, str):
                        errors.append(f"actions[{i}].type: must be a string")
                    elif action_type not in ('link', 'copy', 'run'):
                        errors.append(f"actions[{i}].type: unknown type '{action_type}'")

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

                if 'hidden' in action:
                    if not isinstance(action['hidden'], bool):
                        errors.append(f"actions[{i}].hidden: must be a boolean")

                if 'elevated' in action:
                    if not isinstance(action['elevated'], bool):
                        errors.append(f"actions[{i}].elevated: must be a boolean")

    return errors


def get_module_name(file_path: Path, base_dir: Path) -> Optional[str]:
    rel_path = file_path.relative_to(base_dir)
    parts = rel_path.parts

    if len(parts) <= 1:
        return None

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

    # Track manifest configs for uniqueness check
    manifest_configs: List[Path] = []

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

        # Collect manifest configs
        if data.get('schema') == 'manifest':
            manifest_configs.append(file_path)

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

    # Check for manifest in module directory (not in root)
    for manifest_file in manifest_configs:
        rel_path = manifest_file.relative_to(base_dir)
        parts = rel_path.parts
        if len(parts) > 1:
            # Manifest is inside a subdirectory
            all_valid = False
            module_name = parts[0]
            file_results.append({
                "path": module_name,
                "valid": False,
                "errors": [f"manifest schema must be in the root directory, found in '{module_name}'"]
            })

    # Check for multiple manifest configs in root
    root_manifests = [m for m in manifest_configs if len(m.relative_to(base_dir).parts) == 1]
    if len(root_manifests) > 1:
        all_valid = False
        for manifest_file in root_manifests:
            rel_path = manifest_file.relative_to(base_dir)
            file_results.append({
                "path": str(rel_path),
                "valid": False,
                "errors": ["multiple 'manifest' schemas found — only one is allowed"]
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
        elif schema == 'environments':
            errors = validate_environments_config(data)
        elif schema == 'manifest':
            errors = validate_manifest_config(data)
        else:
            # Unknown schema or missing schema - validate as module for base checks
            errors = validate_module_config(data, file_path, base_dir, module_dirs)

        # Validate depends_on on all configs
        errors.extend(validate_depends_on(data))

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

    try:
        entries = sorted(os.listdir(module_dir))
    except OSError:
        return []

    for entry in entries:
        entry_path = module_dir / entry

        if entry_path.is_dir():
            continue

        if entry.endswith(('.yaml', '.yml', '.json')):
            continue

        if entry.startswith('.'):
            continue

        dest_filename = entry
        if hidden and not dest_filename.startswith('.'):
            dest_filename = '.' + dest_filename

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
    dest_to_sources: Dict[str, List[Tuple[str, str, str]]] = {}

    for module, action_type, source, destination in file_actions:
        if destination not in dest_to_sources:
            dest_to_sources[destination] = []
        dest_to_sources[destination].append((module, action_type, source))

    conflicts = []
    for dest, sources in dest_to_sources.items():
        if len(sources) > 1:
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
        conflicts.sort(key=lambda x: x['destination'])
        return {
            "error": "conflict",
            "conflicts": conflicts
        }

    return None


def find_cycle(module: str, dependencies: Dict[str, set], visited: set, rec_stack: set, path: List[str]) -> Optional[List[str]]:
    """Find a cycle in the dependency graph using DFS.

    Returns the cycle path if found, None otherwise.
    """
    visited.add(module)
    rec_stack.add(module)
    path.append(module)

    for dep in dependencies.get(module, set()):
        if dep not in visited:
            cycle = find_cycle(dep, dependencies, visited, rec_stack, path)
            if cycle:
                return cycle
        elif dep in rec_stack:
            cycle_start = path.index(dep)
            return path[cycle_start:] + [dep]

    path.pop()
    rec_stack.remove(module)
    return None


def detect_circular_dependency(dependencies: Dict[str, set]) -> Optional[List[str]]:
    """Detect if there's a circular dependency.

    Returns the cycle if found, None otherwise.
    """
    visited: set = set()
    rec_stack: set = set()

    for module in dependencies:
        if module not in visited:
            cycle = find_cycle(module, dependencies, visited, rec_stack, [])
            if cycle:
                return cycle

    return None


def topological_sort(modules: List[str], dependencies: Dict[str, set], profile_order: Optional[List[str]] = None) -> List[str]:
    """Topologically sort modules with tie-breaking.

    Every module appears after all its dependencies.
    When multiple modules are eligible:
    - If profile_order is provided, use it for tie-breaking (modules appear in profile order)
    - Otherwise, pick alphabetically
    """
    result = []
    remaining = set(modules)

    # Create a lookup for profile order position
    profile_position = {}
    if profile_order:
        for i, mod in enumerate(profile_order):
            profile_position[mod] = i

    while remaining:
        eligible = []
        for module in remaining:
            deps = dependencies.get(module, set())
            if deps.issubset(set(result)):
                eligible.append(module)

        if not eligible:
            result.extend(sorted(remaining))
            break

        if profile_order and profile_position:
            # Sort by profile order position, then alphabetically for modules not in profile
            eligible.sort(key=lambda m: (profile_position.get(m, float('inf')), m))
        else:
            eligible.sort()
        chosen = eligible[0]
        result.append(chosen)
        remaining.remove(chosen)

    return result


def generate_environment_actions(env_config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Generate environment actions from an environment config."""
    actions = []

    language = env_config.get('language')
    versions = env_config.get('versions', [])
    manager = env_config.get('manager', {})
    manager_name = manager.get('name', '')
    plugins = manager.get('plugins', [])

    for version in versions:
        actions.append({
            "type": "install_runtime",
            "language": language,
            "version": version,
            "manager": manager_name
        })

    for plugin in plugins:
        plugin_name = plugin.get('name', '')
        actions.append({
            "type": "install_plugin",
            "manager": manager_name,
            "plugin": plugin_name
        })

        virtual_envs = plugin.get('virtual_environments', [])
        for venv in virtual_envs:
            actions.append({
                "type": "create_virtual_env",
                "language": language,
                "manager": manager_name,
                "plugin": plugin_name,
                "version": venv.get('version'),
                "name": venv.get('name')
            })

    return actions


def generate_plan(base_dir: Path, os_filter: Optional[str] = None, module_filter: Optional[List[str]] = None, os_version_filter: Optional[str] = None, profile_filter: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Generate the provisioning plan.

    Returns:
        Tuple of (plan, error). On success, plan is returned and error is None.
        On failure, plan is None and error is returned.
    """
    if os_version_filter is not None:
        if version_index(os_version_filter) == -1:
            return None, {
                "error": "unknown_os_version",
                "details": f"unknown version '{os_version_filter}'"
            }

    # Handle manifest validation
    manifest_data, manifest_error = validate_manifest(base_dir)
    if manifest_error:
        return None, manifest_error

    # Handle profile filter
    profile_modules = None
    profile_order = None

    if profile_filter is not None:
        if manifest_data is None:
            return None, {
                "error": "no_manifest",
                "details": "no manifest found but --profile was specified"
            }

        profiles = manifest_data.get('profiles', {})
        if profile_filter not in profiles:
            return None, {
                "error": "unknown_profile",
                "details": f"unknown profile '{profile_filter}'"
            }

        profile_modules = resolve_profile(profile_filter, profiles)
        profile_order = profile_modules  # Keep order for tie-breaking

    validation_result = validate_all_configs(base_dir)

    if not validation_result["valid"]:
        return None, {
            "error": "validation_failed",
            "details": validation_result
        }

    config_files = discover_config_files(base_dir)

    module_configs: Dict[str, Dict[str, Any]] = {}
    module_preferences: Dict[str, List[Dict[str, Any]]] = {}
    module_dock: Optional[Tuple[str, Dict[str, Any]]] = None
    module_environments: Dict[str, List[Dict[str, Any]]] = {}
    module_depends_on: Dict[str, set] = {}

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
            for pref in data.get('preferences', []):
                module_preferences[module_name].append(pref)
        elif schema == 'dock':
            module_dock = (module_name, data)
        elif schema == 'environments':
            if module_name not in module_environments:
                module_environments[module_name] = []
            module_environments[module_name].append(data)

        if 'depends_on' in data:
            depends_on = data['depends_on']
            if isinstance(depends_on, list):
                if module_name not in module_depends_on:
                    module_depends_on[module_name] = set()
                for dep in depends_on:
                    if isinstance(dep, str) and dep:
                        module_depends_on[module_name].add(dep)

    all_module_names = set(module_configs.keys()) | set(module_preferences.keys()) | set(module_environments.keys())
    if module_dock:
        all_module_names.add(module_dock[0])

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

    # Check for invalid profile (missing modules)
    if profile_modules is not None:
        missing_modules = sorted(set(profile_modules) - all_module_names)
        if missing_modules:
            return None, {
                "error": "invalid_profile",
                "profile": profile_filter,
                "missing_modules": missing_modules
            }

    filtered_modules = []

    for module_name in all_module_names:
        module_data = module_configs.get(module_name, {})

        if os_filter is not None:
            os_config = module_data.get('os')
            if os_config and isinstance(os_config, dict):
                module_os = os_config.get('name')
                if module_os and module_os != os_filter:
                    continue

        # Apply profile filter first
        if profile_modules is not None:
            if module_name not in profile_modules:
                continue

        # Then apply module filter (intersection)
        if module_filter is not None:
            if module_name not in module_filter:
                continue

        filtered_modules.append(module_name)

    # Add dependencies of filtered modules
    modules_with_deps = set(filtered_modules)
    changed = True
    while changed:
        changed = False
        for module_name in list(modules_with_deps):
            deps = module_depends_on.get(module_name, set())
            for dep in deps:
                if dep not in modules_with_deps and dep in all_module_names:
                    modules_with_deps.add(dep)
                    changed = True

    filtered_modules = list(modules_with_deps)

    # Update profile_order to include auto-added dependencies
    if profile_order is not None:
        # Dependencies are added at the end, preserving profile order for original modules
        # But dependencies need to come before modules that depend on them
        # We'll handle this in the topological sort
        profile_order_with_deps = list(profile_order)
        for mod in filtered_modules:
            if mod not in profile_order_with_deps:
                profile_order_with_deps.append(mod)
        profile_order = profile_order_with_deps

    for module_name in filtered_modules:
        deps = module_depends_on.get(module_name, set())
        for dep in deps:
            if dep not in filtered_modules:
                return None, {
                    "error": "missing_dependency",
                    "module": module_name,
                    "missing": dep
                }

    filtered_dependencies: Dict[str, set] = {}
    for module_name in filtered_modules:
        deps = module_depends_on.get(module_name, set())
        filtered_dependencies[module_name] = deps.intersection(set(filtered_modules))

    cycle = detect_circular_dependency(filtered_dependencies)
    if cycle:
        return None, {
            "error": "circular_dependency",
            "cycle": cycle
        }

    filtered_modules = topological_sort(filtered_modules, filtered_dependencies, profile_order)

    all_file_actions: List[Tuple[str, str, str, str]] = []

    plan_modules = []

    for module_name in filtered_modules:
        module_data = module_configs.get(module_name, {})
        module_dir_base = base_dir / module_name

        module_dir = module_dir_base
        for file_path in config_files:
            if get_module_name(file_path, base_dir) == module_name:
                if parse_config_file(file_path)[0] and parse_config_file(file_path)[0].get('schema') == 'module':
                    module_dir = file_path.parent
                    break

        actions = []

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

            applications = os_config.get('applications', [])
            if applications:
                for app in sorted(applications):
                    actions.append({
                        "type": "install_application",
                        "manager": pm,
                        "application": app
                    })

        config_actions = module_data.get('actions', [])

        for action in config_actions:
            action_type = action.get('type')
            source = action.get('source', '')
            destination = action.get('destination')
            hidden = action.get('hidden', False)
            elevated = action.get('elevated', False)

            if source == "*":
                if destination:
                    expanded = expand_wildcard_source(
                        module_dir, destination, hidden, elevated, action_type
                    )
                    for exp_action in expanded:
                        if action_type in ('link', 'copy'):
                            all_file_actions.append((module_name, action_type, exp_action['source'], exp_action['destination']))
                        actions.append(exp_action)
            else:
                source_abs = str((module_dir / source).resolve())

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

        prefs = module_preferences.get(module_name, [])
        for pref in prefs:
            if pref.get('enabled') is False:
                continue

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

        if module_dock and module_dock[0] == module_name:
            dock_data = module_dock[1]
            if os_filter is None or os_filter == 'macos':
                actions.append({
                    "type": "configure_dock",
                    "items": dock_data.get('items', [])
                })

        env_configs = module_environments.get(module_name, [])
        for env_config in env_configs:
            for env in env_config.get('environments', []):
                actions.extend(generate_environment_actions(env))

        plan_modules.append({
            "name": module_name,
            "actions": actions
        })

    conflicts = detect_conflicts(all_file_actions)
    if conflicts:
        return None, conflicts

    return {"modules": plan_modules}, None


def cmd_validate(args):
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

    sys.exit(0 if result["valid"] else 1)


def cmd_plan(args):
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
    profile_filter = args.profile

    plan, error = generate_plan(base_dir, os_filter, module_filter, os_version_filter, profile_filter)

    if error:
        print(json.dumps(error), file=sys.stderr)
        sys.exit(1)

    print(json.dumps(plan, indent=2))
    sys.exit(0)


def cmd_list_profiles(args):
    base_dir = Path(args.dir).resolve()

    if not base_dir.exists():
        print(json.dumps({
            "error": "file_not_found",
            "details": f"directory '{args.dir}' not found"
        }), file=sys.stderr)
        sys.exit(1)

    # Validate manifest
    manifest_data, manifest_error = validate_manifest(base_dir)
    if manifest_error:
        print(json.dumps(manifest_error), file=sys.stderr)
        sys.exit(1)

    # No manifest means no profiles
    if manifest_data is None:
        print(json.dumps({"profiles": []}, indent=2))
        sys.exit(0)

    # Get all module names for validation
    config_files = discover_config_files(base_dir)
    all_module_names = set()

    for file_path in config_files:
        data, parse_error = parse_config_file(file_path)
        if parse_error or not is_rig_config(data):
            continue

        module_name = get_module_name(file_path, base_dir)
        if module_name:
            all_module_names.add(module_name)

    profiles = manifest_data.get('profiles', {})

    # Check each profile for missing modules
    profile_names = sorted(profiles.keys())
    for profile_name in profile_names:
        resolved_modules = resolve_profile(profile_name, profiles)
        missing_modules = sorted(set(resolved_modules) - all_module_names)
        if missing_modules:
            print(json.dumps({
                "error": "invalid_profile",
                "profile": profile_name,
                "missing_modules": missing_modules
            }), file=sys.stderr)
            sys.exit(1)

    # Build output
    result_profiles = []
    for profile_name in profile_names:
        resolved_modules = resolve_profile(profile_name, profiles)
        result_profiles.append({
            "name": profile_name,
            "modules": sorted(resolved_modules)
        })

    print(json.dumps({"profiles": result_profiles}, indent=2))
    sys.exit(0)


def main():
    parser = argparse.ArgumentParser(description='System Provisioning Planner MVP')
    subparsers = parser.add_subparsers(dest='command', help='Commands')

    validate_parser = subparsers.add_parser('validate', help='Validate config files')
    validate_parser.add_argument('dir', help='Directory to validate')

    plan_parser = subparsers.add_parser('plan', help='Generate provisioning plan')
    plan_parser.add_argument('dir', help='Directory to plan')
    plan_parser.add_argument('--os', dest='os', help='Filter by OS (macos/linux)')
    plan_parser.add_argument('--module', dest='module', nargs='+', help='Filter by module name')
    plan_parser.add_argument('--os-version', dest='os_version', help='Filter by OS version for preferences')
    plan_parser.add_argument('--profile', dest='profile', help='Filter by profile name')

    list_profiles_parser = subparsers.add_parser('list-profiles', help='List available profiles')
    list_profiles_parser.add_argument('dir', help='Directory to inspect')

    args = parser.parse_args()

    if args.command == 'validate':
        cmd_validate(args)
    elif args.command == 'plan':
        cmd_plan(args)
    elif args.command == 'list-profiles':
        cmd_list_profiles(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
