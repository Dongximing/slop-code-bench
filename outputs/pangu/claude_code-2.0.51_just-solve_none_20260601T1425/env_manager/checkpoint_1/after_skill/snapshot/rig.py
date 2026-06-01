#!/usr/bin/env python3
"""System Provisioning Planner MVP - rig.py"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

import yaml


class ValidationError:
    """Represents a validation error for a config file."""
    def __init__(self, message: str):
        self.message = message


def parse_config_file(filepath: Path) -> tuple[dict[str, Any] | None, list[ValidationError]]:
    """Parse a YAML/JSON config file. Returns (config, errors)."""
    errors = []
    config = None

    try:
        with open(filepath, 'r') as f:
            content = f.read()
            if filepath.suffix in ('.yaml', '.yml'):
                config = yaml.safe_load(content)
            else:  # .json
                config = json.loads(content)
    except (yaml.YAMLError, json.JSONDecodeError) as e:
        errors.append(ValidationError(f"parse error: {e}"))
        return None, errors
    except Exception as e:
        errors.append(ValidationError(f"parse error: {e}"))
        return None, errors

    if config is None:
        config = {}

    return config, errors


def validate_config(config: dict[str, Any], filepath: Path) -> list[ValidationError]:
    """Validate a parsed config. Returns list of errors."""
    errors = []

    # 1. Validate version
    if 'version' not in config:
        errors.append(ValidationError('version required'))
    else:
        version = config['version']
        if not isinstance(version, str):
            errors.append(ValidationError('version: must be a string'))
        elif version != '1':
            errors.append(ValidationError(f"unsupported version '{version}'"))

    # 2. Validate schema
    if 'schema' not in config:
        if 'version' in config and isinstance(config['version'], str) and config['version'] == '1':
            errors.append(ValidationError('schema required'))
    else:
        schema = config['schema']
        if not isinstance(schema, str):
            errors.append(ValidationError('schema: must be a string'))
        elif schema not in ('module',):
            errors.append(ValidationError(f"unknown schema '{schema}'"))

    # 3. Validate os
    if 'os' in config:
        os_config = config['os']
        if not isinstance(os_config, dict):
            errors.append(ValidationError('os: must be an object'))
        else:
            # Validate os.name
            if 'name' not in os_config:
                errors.append(ValidationError('os: name required'))
            else:
                os_name = os_config['name']
                if not isinstance(os_name, str):
                    errors.append(ValidationError('os.name: must be a string'))
                elif os_name not in ('macos', 'linux'):
                    errors.append(ValidationError(f"os.name: invalid value '{os_name}'"))

            # Validate os.package_manager
            if 'package_manager' in os_config:
                pm = os_config['package_manager']
                if not isinstance(pm, str):
                    errors.append(ValidationError('os.package_manager: must be a string'))
                elif pm not in ('brew', 'apt', 'yum'):
                    errors.append(ValidationError(f"os.package_manager: invalid value '{pm}'"))
            elif 'packages' in os_config or 'applications' in os_config:
                errors.append(ValidationError('os.package_manager: required when packages or applications present'))

            # Validate os.packages
            if 'packages' in os_config:
                packages = os_config['packages']
                if not isinstance(packages, list):
                    errors.append(ValidationError('os.packages: must be a list'))
                else:
                    for i, pkg in enumerate(packages):
                        if not isinstance(pkg, str):
                            errors.append(ValidationError(f'os.packages[{i}]: must be a string'))
                        elif not pkg:
                            errors.append(ValidationError(f'os.packages[{i}]: must not be empty'))

            # Validate os.applications
            if 'applications' in os_config:
                apps = os_config['applications']
                if not isinstance(apps, list):
                    errors.append(ValidationError('os.applications: must be a list'))
                else:
                    for i, app in enumerate(apps):
                        if not isinstance(app, str):
                            errors.append(ValidationError(f'os.applications[{i}]: must be a string'))
                        elif not app:
                            errors.append(ValidationError(f'os.applications[{i}]: must not be empty'))

    # 4. Validate actions
    if 'actions' in config:
        actions = config['actions']
        if not isinstance(actions, list):
            errors.append(ValidationError('actions: must be a list'))
        else:
            for i, action in enumerate(actions):
                if not isinstance(action, dict):
                    errors.append(ValidationError(f'actions[{i}]: must be an object'))
                    continue

                # Validate action type
                if 'type' not in action:
                    errors.append(ValidationError(f'actions[{i}]: type required'))
                else:
                    action_type = action['type']
                    if not isinstance(action_type, str):
                        errors.append(ValidationError(f'actions[{i}].type: must be a string'))
                    elif action_type not in ('link', 'copy', 'run'):
                        errors.append(ValidationError(f'actions[{i}].type: invalid value "{action_type}"'))

                # Validate action source
                if 'source' not in action:
                    errors.append(ValidationError(f'actions[{i}]: source required'))
                else:
                    source = action['source']
                    if not isinstance(source, str):
                        errors.append(ValidationError(f'actions[{i}].source: must be a string'))
                    elif not source:
                        errors.append(ValidationError(f'actions[{i}].source: must not be empty'))
                    elif source.startswith('/'):
                        errors.append(ValidationError(f'actions[{i}].source: must not start with /'))

                # Validate action destination
                if 'destination' in action:
                    dest = action['destination']
                    if not isinstance(dest, str):
                        errors.append(ValidationError(f'actions[{i}].destination: must be a string'))
                    elif not dest:
                        errors.append(ValidationError(f'actions[{i}].destination: must not be empty'))
                    elif not (dest.startswith('/') or dest.startswith('~')):
                        errors.append(ValidationError(f'actions[{i}].destination: must start with / or ~'))
                    elif action.get('source') == '*' and not dest.endswith('/'):
                        errors.append(ValidationError(f'actions[{i}].destination: must end with / for wildcard'))
                elif action.get('type') in ('link', 'copy'):
                    errors.append(ValidationError(f'actions[{i}]: destination required for {action.get("type")}'))

                # Validate hidden and elevated
                if 'hidden' in action:
                    if not isinstance(action['hidden'], bool):
                        errors.append(ValidationError(f'actions[{i}].hidden: must be a boolean'))
                if 'elevated' in action:
                    if not isinstance(action['elevated'], bool):
                        errors.append(ValidationError(f'actions[{i}].elevated: must be a boolean'))

    return errors


def discover_configs(root_dir: Path) -> list[tuple[Path, dict[str, Any] | None, list[ValidationError]]]:
    """Discover all rig configs in a directory."""
    results = []

    for file_path in root_dir.rglob('*'):
        # Skip .git directories
        if '.git' in file_path.parts:
            continue

        # Check for valid extensions
        if file_path.suffix.lower() not in ('.yaml', '.yml', '.json'):
            continue

        # Parse the file
        config, parse_errors = parse_config_file(file_path)

        # If parsing failed, it's still a "discovered" file but invalid
        if parse_errors:
            results.append((file_path, config, parse_errors))
            continue

        # Check if it has version (rig config requirement)
        if config and 'version' in config:
            validation_errors = validate_config(config, file_path)
            results.append((file_path, config, validation_errors))
        # Non-versioned files are ignored (not included in results)

    return results


def get_module_name(config_dir: Path, root_dir: Path) -> str:
    """Get module name from directory path relative to root."""
    return str(config_dir.relative_to(root_dir))


def expand_wildcard(module_dir: Path, source: str, destination: str, hidden: bool, elevated: bool) -> list[dict]:
    """Expand wildcard source into concrete actions."""
    actions = []

    if not module_dir.exists():
        return actions

    matched_files = []
    for item in module_dir.iterdir():
        # Skip directories
        if item.is_dir():
            continue
        # Skip hidden files
        if item.name.startswith('.'):
            continue
        # Skip yaml/yml/json files
        if item.suffix.lower() in ('.yaml', '.yml', '.json'):
            continue
        matched_files.append(item)

    # Sort by filename
    matched_files.sort(key=lambda x: x.name)

    for file_path in matched_files:
        dest_path = destination + file_path.name

        # Apply hidden modifier
        if hidden and not file_path.name.startswith('.'):
            dest_path = os.path.dirname(dest_path) + '/.' + os.path.basename(dest_path)

        # Resolve ~ in destination
        if dest_path.startswith('~'):
            dest_path = os.path.expanduser(dest_path)

        actions.append({
            'type': 'link',
            'source': str(file_path.absolute()),
            'destination': dest_path,
            'elevated': elevated
        })

    return actions


def process_actions(
    config: dict[str, Any],
    module_dir: Path,
    os_filter: Optional[str],
    elevated_default: bool = False
) -> tuple[list[dict], Optional[str]]:
    """Process actions from config into plan actions. Returns (actions, error)."""
    actions = []
    errors = []

    # Check OS filter
    os_config = config.get('os', {})
    if os_filter and 'name' in os_config and os_config['name'] != os_filter:
        return [], None  # Skip this module

    pm = os_config.get('package_manager', '')

    # Add package install actions
    packages = os_config.get('packages', [])
    for pkg in sorted(packages):
        actions.append({
            'type': 'install_package',
            'manager': pm,
            'package': pkg
        })

    # Add application install actions
    applications = os_config.get('applications', [])
    for app in sorted(applications):
        actions.append({
            'type': 'install_application',
            'manager': pm,
            'application': app
        })

    # Process file actions
    for action in config.get('actions', []):
        action_type = action.get('type', '')
        source = action.get('source', '')
        is_wildcard = source == '*'

        # Resolve source path
        if is_wildcard:
            destination = action.get('destination', '')
            hidden = action.get('hidden', False)
            elevated = action.get('elevated', elevated_default)

            expanded = expand_wildcard(module_dir, source, destination, hidden, elevated)
            actions.extend(expanded)
        else:
            # Non-wildcard action
            source_path = module_dir / source

            # Check if source exists
            if not source_path.exists():
                errors.append(f"missing source file '{source}'")
                return [], f"missing source file '{source}'"

            # Resolve paths
            resolved_source = str(source_path.absolute())
            elevated = action.get('elevated', elevated_default)

            if action_type == 'link':
                destination = action.get('destination', '')
                if destination.startswith('~'):
                    destination = os.path.expanduser(destination)
                actions.append({
                    'type': 'link',
                    'source': resolved_source,
                    'destination': destination,
                    'elevated': elevated
                })
            elif action_type == 'copy':
                destination = action.get('destination', '')
                if destination.startswith('~'):
                    destination = os.path.expanduser(destination)
                actions.append({
                    'type': 'copy',
                    'source': resolved_source,
                    'destination': destination,
                    'elevated': elevated
                })
            elif action_type == 'run':
                actions.append({
                    'type': 'run',
                    'source': resolved_source,
                    'elevated': elevated
                })

    if errors:
        return [], errors[0]

    return actions, None


def check_duplicate_modules(configs: list[tuple[Path, dict, list]]) -> Optional[str]:
    """Check for duplicate module schemas in directories."""
    module_dirs = {}

    for file_path, config, errors in configs:
        if config and 'schema' in config and config['schema'] == 'module':
            module_dir = file_path.parent
            module_name = get_module_name(module_dir, file_path.parent.parent)  # Get relative to root

            # For root-level configs, don't treat as modules
            if module_dir == file_path.parent.parent:
                continue

            if module_dir not in module_dirs:
                module_dirs[module_dir] = []
            module_dirs[module_dir].append(file_path.name)

    for module_dir, files in module_dirs.items():
        if len(files) > 1:
            module_name = get_module_name(module_dir, module_dir.parent)
            return f"duplicate 'module' schema in '{module_name}'"

    return None


def generate_plan(
    root_dir: Path,
    os_filter: Optional[str] = None,
    module_filter: Optional[list[str]] = None
) -> tuple[dict, Optional[str]]:
    """Generate provisioning plan. Returns (plan, error_message)."""
    # Discover configs
    discovered = discover_configs(root_dir)

    # Check for validation errors
    validation_errors = []
    valid_configs = []
    for file_path, config, errors in discovered:
        if errors:
            validation_errors.append((file_path, config, errors))
        elif config and 'schema' in config and config['schema'] == 'module':
            valid_configs.append((file_path, config, errors))

    # Check for duplicate modules
    dup_error = check_duplicate_modules(discovered)
    if dup_error:
        # Report as validation error
        for file_path, config, errors in discovered:
            if config and 'schema' in config and config['schema'] == 'module':
                validation_errors.append((file_path, config, [ValidationError(dup_error)]))

    if validation_errors:
        files_result = []
        for file_path, config, errors in discovered:
            file_entry = {
                'path': str(file_path.relative_to(root_dir)),
                'valid': len(errors) == 0
            }
            if errors:
                file_entry['errors'] = [e.message for e in errors]
            files_result.append(file_entry)

        files_result.sort(key=lambda x: x['path'])
        validate_result = {'valid': False, 'files': files_result}
        return None, ('validation_failed', json.dumps(validate_result))

    if not valid_configs and not any(c for _, c, _ in discovered):
        return {'modules': []}, None

    # Filter and process modules
    modules = []
    seen_module_dirs = set()

    for file_path, config, errors in valid_configs:
        module_dir = file_path.parent
        module_name = get_module_name(module_dir, root_dir)

        # Skip root-level configs
        if module_dir == root_dir:
            continue

        # Skip duplicate module directories
        if module_dir in seen_module_dirs:
            continue
        seen_module_dirs.add(module_dir)

        # Apply OS filter
        os_config = config.get('os', {})
        if os_filter and 'name' in os_config and os_config['name'] != os_filter:
            continue

        # Apply module filter
        if module_filter and module_name not in module_filter:
            continue

        # Process actions
        actions, file_error = process_actions(config, module_dir, os_filter)

        if file_error:
            return None, ('file_not_found', f"module '{module_name}': {file_error}")

        modules.append({
            'name': module_name,
            'actions': actions
        })

    # Sort modules by name
    modules.sort(key=lambda x: x['name'])

    return {'modules': modules}, None


def cmd_validate(args):
    """Validate rig configs in a directory."""
    root_dir = Path(args.dir).resolve()

    if not root_dir.exists():
        print(json.dumps({'valid': False, 'errors': ['directory not found']}), file=sys.stderr)
        sys.exit(1)

    discovered = discover_configs(root_dir)

    files_result = []
    all_valid = True

    for file_path, config, errors in discovered:
        file_entry = {
            'path': str(file_path.relative_to(root_dir)),
            'valid': len(errors) == 0
        }
        if errors:
            file_entry['errors'] = [e.message for e in errors]
            all_valid = False
        files_result.append(file_entry)

    files_result.sort(key=lambda x: x['path'])

    result = {'valid': all_valid, 'files': files_result}
    print(json.dumps(result, indent=2))

    sys.exit(0 if all_valid else 1)


def cmd_plan(args):
    """Generate provisioning plan."""
    root_dir = Path(args.dir).resolve()

    if not root_dir.exists():
        error = {'error': 'file_not_found', 'details': f"directory '{args.dir}' not found"}
        print(json.dumps(error), file=sys.stderr)
        sys.exit(1)

    os_filter = args.os
    module_filter = args.module

    plan, error = generate_plan(root_dir, os_filter, module_filter)

    if error:
        error_type, details = error
        error_obj = {'error': error_type, 'details': details}
        print(json.dumps(error_obj), file=sys.stderr)
        sys.exit(1)

    print(json.dumps(plan, indent=2))
    sys.exit(0)


def main():
    parser = argparse.ArgumentParser(description='System Provisioning Planner')
    subparsers = parser.add_subparsers(dest='command', required=True)

    # Validate command
    validate_parser = subparsers.add_parser('validate', help='Validate rig configs')
    validate_parser.add_argument('dir', help='Directory to validate')
    validate_parser.set_defaults(func=cmd_validate)

    # Plan command
    plan_parser = subparsers.add_parser('plan', help='Generate provisioning plan')
    plan_parser.add_argument('dir', help='Directory to plan')
    plan_parser.add_argument('--os', help='Filter by OS (macos or linux)')
    plan_parser.add_argument('--module', nargs='*', help='Filter by module name(s)')
    plan_parser.set_defaults(func=cmd_plan)

    args = parser.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
