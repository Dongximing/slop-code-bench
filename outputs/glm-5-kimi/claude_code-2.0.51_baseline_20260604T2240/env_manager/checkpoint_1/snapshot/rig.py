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

    for root, dirs, files in os.walk(directory):
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
    elif config['schema'] != 'module':
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
    Validate all configs and check for duplicate module schemas.

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

        # Determine module directory for this config
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

    # Find module configs (directories with schema: module)
    modules = []
    dir_to_module_config = {}

    for config_info in configs:
        if config_info['parse_error'] or not config_info['content']:
            continue

        content = config_info['content']
        if content.get('schema') == 'module':
            config_path = Path(config_info['path'])
            module_dir = config_path.parent

            # Root-level configs are not modules
            if str(module_dir) == '.':
                continue

            module_name = str(module_dir)

            # Check for duplicate module schema (should not happen after validation)
            if module_dir in dir_to_module_config:
                continue

            dir_to_module_config[module_dir] = {
                'name': module_name,
                'content': content,
                'config_path': config_info['path']
            }

    # Apply filters
    filtered_modules = []
    for module_dir, module_data in dir_to_module_config.items():
        module_name = module_data['name']
        content = module_data['content']

        # OS filter
        if os_filter is not None:
            module_os = content.get('os', {}).get('name')
            if module_os is not None and module_os != os_filter:
                continue

        # Module name filter
        if module_filter is not None:
            if module_name not in module_filter:
                continue

        filtered_modules.append(module_data)

    # Sort modules alphabetically
    filtered_modules.sort(key=lambda m: m['name'])

    # Generate plan for each module
    plan_modules = []

    for module_data in filtered_modules:
        module_name = module_data['name']
        content = module_data['content']
        config_path = Path(module_data['config_path'])
        module_dir = config_path.parent

        actions = []

        # Process os packages and applications
        os_config = content.get('os', {})
        package_manager = os_config.get('package_manager')

        if package_manager:
            # Install packages (sorted by package name)
            packages = os_config.get('packages', [])
            for pkg in sorted(packages):
                actions.append({
                    'type': 'install_package',
                    'manager': package_manager,
                    'package': pkg
                })

            # Install applications (sorted by application name)
            applications = os_config.get('applications', [])
            for app in sorted(applications):
                actions.append({
                    'type': 'install_application',
                    'manager': package_manager,
                    'application': app
                })

        # Process file actions
        config_actions = content.get('actions', [])
        file_actions = []

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
                    file_actions.append({
                        'type': 'run',
                        'source': abs_source,
                        'elevated': exp_action.get('elevated', False)
                    })
                else:
                    destination = exp_action.get('destination', '')
                    hidden = exp_action.get('hidden', False)
                    resolved_dest = resolve_destination(destination, hidden)

                    file_actions.append({
                        'type': action_type,
                        'source': abs_source,
                        'destination': resolved_dest,
                        'elevated': exp_action.get('elevated', False)
                    })

        # Append file actions to module actions (in config order)
        actions.extend(file_actions)

        plan_modules.append({
            'name': module_name,
            'actions': actions
        })

    return (True, {'modules': plan_modules})


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


def cmd_plan(directory: str, os_filter: Optional[str] = None, module_filter: Optional[List[str]] = None) -> int:
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

    # Check for parse errors (should trigger validation_failed)
    has_parse_errors = any(c['parse_error'] for c in configs)

    success, result = generate_plan(configs, base_dir, os_filter, module_filter)

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
            print("Usage: rig.py plan <dir> [--os <name>] [--module <name> ...]", file=sys.stderr)
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
            elif sys.argv[i] == '--module':
                module_filter = []
                i += 1
                while i < len(sys.argv) and not sys.argv[i].startswith('--'):
                    module_filter.append(sys.argv[i])
                    i += 1
            else:
                i += 1

        return cmd_plan(directory, os_filter, module_filter)

    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())