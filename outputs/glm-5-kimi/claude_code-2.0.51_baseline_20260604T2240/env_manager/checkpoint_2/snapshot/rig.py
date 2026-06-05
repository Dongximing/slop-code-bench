#!/usr/bin/env python3
"""
System Provisioning Planner MVP

A dry-run CLI that discovers rig config files, validates them, and generates
a deterministic plan for package installs, app installs, and file actions.
"""

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
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
    elif config['schema'] not in ('module', 'preferences', 'dock'):
        errors.append(f"unknown schema '{config['schema']}'")

    # Early exit if schema is not module
    if config.get('schema') != 'module':
        return (len(errors) == 0, errors)

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
        return (len(errors) == 0, errors)

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
        return (len(errors) == 0, errors)

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
                'dock': None
            }

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

    # Build modules list - a directory is a module if it has module config OR preferences/dock config
    modules_data = []

    for module_dir, configs_data in dir_to_configs.items():
        module_name = str(module_dir)

        # Check if this directory has any configs that make it a module
        has_module = configs_data['module'] is not None
        has_preferences = len(configs_data['preferences']) > 0
        has_dock = configs_data['dock'] is not None

        if not (has_module or has_preferences or has_dock):
            continue

        module_content = configs_data['module']['content'] if configs_data['module'] else None

        # OS filter - check module's os.name
        if os_filter is not None and module_content is not None:
            module_os = module_content.get('os', {}).get('name')
            if module_os is not None and module_os != os_filter:
                # Module's os.name doesn't match filter
                # Only skip if this module would have no other actions (preferences)
                if not has_preferences and not has_dock:
                    continue
                # If it only has dock and --os is not macos, skip
                if not has_preferences and has_dock and os_filter != 'macos':
                    continue

        # Also apply OS filter to modules without module.yaml
        # For preferences-only or dock-only modules, skip if --os linux and they're macOS-only
        if os_filter is not None and module_content is None:
            # No module config, just preferences/dock
            # Preferences are not OS-specific (only version-specific), so keep them
            # Dock is macOS-only, so skip if --os is not macos
            if has_dock and os_filter != 'macos' and not has_preferences:
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
            'has_module': has_module
        })

    # Sort modules alphabetically
    modules_data.sort(key=lambda m: m['name'])

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

    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())