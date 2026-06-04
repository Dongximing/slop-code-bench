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


def discover_config_files(base_dir: str) -> list[tuple[str, dict]]:
    """Recursively discover config files in base_dir.

    Returns list of (relative_path, parsed_content) tuples.
    Non-versioned files and parse errors are excluded from returned content.
    """
    base_path = Path(base_dir).resolve()
    configs = []

    for root, dirs, files in os.walk(base_path):
        # Skip .git directories
        dirs[:] = [d for d in dirs if d != '.git']

        for file in files:
            ext = Path(file).suffix.lower()
            if ext not in ('.yaml', '.yml', '.json'):
                continue

            file_path = Path(root) / file
            rel_path = str(file_path.relative_to(base_path))

            try:
                with open(file_path, 'r') as f:
                    content = yaml.safe_load(f)
            except (yaml.YAMLError, UnicodeDecodeError) as e:
                # Parse errors are handled during validation
                continue

            if content is None:
                continue

            # Check if it has top-level version
            if 'version' in content:
                configs.append((rel_path, content))

    return configs


def validate_config(content: dict, rel_path: str, base_dir: str) -> list[ValidationError]:
    """Validate a config file according to the specification."""
    errors = []

    # 1. version type and value check
    if 'version' not in content:
        errors.append(ValidationError('version: missing'))
    else:
        version = content['version']
        if not isinstance(version, str):
            errors.append(ValidationError('version: must be a string'))
        elif version != '1':
            errors.append(ValidationError(f"unsupported version '{version}'"))

    # schema check
    if 'schema' not in content:
        if 'version' in content and isinstance(content['version'], str) and content['version'] == '1':
            errors.append(ValidationError('schema: missing'))
    else:
        schema = content['schema']
        if not isinstance(schema, str):
            errors.append(ValidationError('schema: must be a string'))
        elif schema != 'module':
            errors.append(ValidationError(f'recognized schema "{schema}"'))

    # Skip further validation if version is invalid (unless schema is also missing and we already reported it)
    if 'version' in content and not isinstance(content['version'], str):
        return errors
    if 'version' in content and content['version'] != '1':
        return errors

    # os type check
    if 'os' in content:
        os_val = content['os']
        if not isinstance(os_val, dict):
            errors.append(ValidationError('os: must be an object'))
        else:
            # os.name check
            if 'name' not in os_val:
                errors.append(ValidationError('os.name: missing'))
            else:
                os_name = os_val['name']
                if not isinstance(os_name, str):
                    errors.append(ValidationError('os.name: must be a string'))
                elif os_name not in ('macos', 'linux'):
                    errors.append(ValidationError('os.name: must be "macos" or "linux"'))

            # os.package_manager check
            if 'package_manager' in os_val:
                pm = os_val['package_manager']
                if not isinstance(pm, str):
                    errors.append(ValidationError('os.package_manager: must be a string'))
                elif pm not in ('brew', 'apt', 'yum'):
                    errors.append(ValidationError('os.package_manager: must be "brew", "apt", or "yum"'))

            # Check package_manager requirement when packages/apps present
            has_packages = 'packages' in os_val
            has_apps = 'applications' in os_val

            if has_packages or has_apps:
                if 'package_manager' not in os_val:
                    errors.append(ValidationError('os.package_manager: required when packages or applications present'))
                elif isinstance(os_val.get('package_manager'), str) and os_val['package_manager'] not in ('brew', 'apt', 'yum'):
                    # package_manager error already reported above
                    pass

            # os.packages check
            if 'packages' in os_val:
                packages = os_val['packages']
                if not isinstance(packages, list):
                    errors.append(ValidationError('os.packages: must be a list'))
                else:
                    for i, pkg in enumerate(packages):
                        if not isinstance(pkg, str):
                            errors.append(ValidationError(f'os.packages[{i}]: must be a string'))
                        elif not pkg:
                            errors.append(ValidationError(f'os.packages[{i}]: must be non-empty'))

            # os.applications check
            if 'applications' in os_val:
                apps = os_val['applications']
                if not isinstance(apps, list):
                    errors.append(ValidationError('os.applications: must be a list'))
                else:
                    for i, app in enumerate(apps):
                        if not isinstance(app, str):
                            errors.append(ValidationError(f'os.applications[{i}]: must be a string'))
                        elif not app:
                            errors.append(ValidationError(f'os.applications[{i}]: must be non-empty'))

    # actions check
    if 'actions' in content:
        actions = content['actions']
        if not isinstance(actions, list):
            errors.append(ValidationError('actions: must be a list'))
        else:
            for i, action in enumerate(actions):
                if not isinstance(action, dict):
                    errors.append(ValidationError(f'actions[{i}]: must be an object'))
                    continue

                # type check
                if 'type' not in action:
                    errors.append(ValidationError(f'actions[{i}].type: missing'))
                else:
                    act_type = action['type']
                    if not isinstance(act_type, str):
                        errors.append(ValidationError(f'actions[{i}].type: must be a string'))
                    elif act_type not in ('link', 'copy', 'run'):
                        errors.append(ValidationError(f'actions[{i}].type: must be "link", "copy", or "run"'))

                # source check
                if 'source' not in action:
                    errors.append(ValidationError(f'actions[{i}].source: missing'))
                else:
                    source = action['source']
                    if not isinstance(source, str):
                        errors.append(ValidationError(f'actions[{i}].source: must be a string'))
                    elif not source:
                        errors.append(ValidationError(f'actions[{i}].source: must be non-empty'))
                    elif source.startswith('/'):
                        errors.append(ValidationError(f'actions[{i}].source: must not start with /'))

                # destination check
                if 'destination' in action:
                    dest = action['destination']
                    act_type = action.get('type')

                    if not isinstance(dest, str):
                        errors.append(ValidationError(f'actions[{i}].destination: must be a string'))
                    elif not dest:
                        errors.append(ValidationError(f'actions[{i}].destination: must be non-empty'))
                    elif not (dest.startswith('/') or dest.startswith('~')):
                        errors.append(ValidationError(f'actions[{i}].destination: must start with / or ~'))
                    elif act_type in ('link', 'copy') and action.get('source') == '*':
                        if not dest.endswith('/'):
                            errors.append(ValidationError(f'actions[{i}].destination: must end with / for wildcard'))

                # hidden type check
                if 'hidden' in action:
                    hidden = action['hidden']
                    if not isinstance(hidden, bool):
                        errors.append(ValidationError(f'actions[{i}].hidden: must be a boolean'))

                # elevated type check
                if 'elevated' in action:
                    elevated = action['elevated']
                    if not isinstance(elevated, bool):
                        errors.append(ValidationError(f'actions[{i}].elevated: must be a boolean'))

    return errors


def resolve_path(path: str, base_dir: Path, is_source: bool = False) -> str:
    """Resolve a path to absolute form."""
    if path.startswith('~'):
        # Expand ~ to home directory
        home = Path.home()
        path = str(home) + path[1:]

    if not (path.startswith('/') or path.startswith('~')):
        # Relative path - resolve relative to base_dir
        path = str(base_dir / path)

    return path


def expand_wildcard(source: str, base_dir: Path) -> list[str]:
    """Expand wildcard source to list of matching files."""
    if source != '*':
        return []

    matches = []
    for item in base_dir.iterdir():
        if item.is_dir():
            continue
        name = item.name
        # Exclude hidden files
        if name.startswith('.'):
            continue
        # Exclude config files
        if item.suffix.lower() in ('.yaml', '.yml', '.json'):
            continue
        matches.append(name)

    return sorted(matches)


def generate_actions(
    module_name: str,
    config: dict,
    module_dir: Path,
    os_filter: Optional[str] = None
) -> list[dict]:
    """Generate action plan for a module."""
    actions = []

    # Check OS filter
    if os_filter:
        module_os = config.get('os', {}).get('name')
        if module_os and module_os != os_filter:
            return []

    pm = config.get('os', {}).get('package_manager', '')

    # Process packages
    packages = config.get('os', {}).get('packages', [])
    for pkg in sorted(packages):
        actions.append({
            'type': 'install_package',
            'manager': pm,
            'package': pkg
        })

    # Process applications
    apps = config.get('os', {}).get('applications', [])
    for app in sorted(apps):
        actions.append({
            'type': 'install_application',
            'manager': pm,
            'application': app
        })

    # Process actions
    file_actions = config.get('actions', [])
    for action in file_actions:
        act_type = action['type']
        source = action['source']
        elevated = action.get('elevated', False)

        if source == '*':
            # Wildcard expansion
            matched_files = expand_wildcard(source, module_dir)
            dest_template = action.get('destination', '')

            for filename in matched_files:
                dest = dest_template + filename

                # Apply hidden modifier
                if action.get('hidden', False):
                    dest_parts = dest.rsplit('/', 1)
                    if len(dest_parts) == 2:
                        dir_part, file_part = dest_parts
                        if not file_part.startswith('.'):
                            file_part = '.' + file_part
                        dest = dir_part + '/' + file_part
                    else:
                        if not dest.startswith('.'):
                            dest = '.' + dest

                dest = resolve_path(dest, Path.home())

                if act_type in ('link', 'copy'):
                    src_path = str(module_dir / filename)
                    actions.append({
                        'type': act_type,
                        'source': src_path,
                        'destination': dest,
                        'elevated': elevated
                    })
        else:
            # Non-wildcard action
            src_path = resolve_path(source, module_dir, is_source=True)

            if act_type == 'run':
                actions.append({
                    'type': 'run',
                    'source': src_path,
                    'elevated': elevated
                })
            else:
                dest = action['destination']
                dest = resolve_path(dest, Path.home())

                # Apply hidden modifier
                if action.get('hidden', False):
                    dest_parts = dest.rsplit('/', 1)
                    if len(dest_parts) == 2:
                        dir_part, file_part = dest_parts
                        if not file_part.startswith('.'):
                            file_part = '.' + file_part
                        dest = dir_part + '/' + file_part
                    else:
                        if not dest.startswith('.'):
                            dest = '.' + dest

                actions.append({
                    'type': act_type,
                    'source': src_path,
                    'destination': dest,
                    'elevated': elevated
                })

    return actions


def check_source_files_exist(config: dict, module_dir: Path) -> list[tuple[str, str]]:
    """Check if non-wildcard source files exist. Returns list of (module_name, filename) for missing files."""
    missing = []
    module_name = module_dir.name

    for action in config.get('actions', []):
        source = action.get('source', '')
        if source == '*':
            continue  # Wildcard is validated at plan time (no match is valid)

        src_path = module_dir / source
        if not src_path.exists():
            missing.append((module_name, source))

    return missing


def validate_command(base_dir: str) -> int:
    """Implement the 'validate' command."""
    base_path = Path(base_dir)
    if not base_path.exists():
        print(json.dumps({"valid": False, "errors": ["directory not found"]}), file=sys.stderr)
        return 1

    configs = discover_config_files(base_dir)
    results = []
    all_valid = True

    for rel_path, content in configs:
        errors = validate_config(content, rel_path, base_dir)
        valid = len(errors) == 0
        if not valid:
            all_valid = False

        file_result = {
            "path": rel_path,
            "valid": valid
        }
        if not valid:
            file_result["errors"] = [e.message for e in errors]

        results.append(file_result)

    # Sort by path
    results.sort(key=lambda x: x["path"])

    output = {
        "valid": all_valid,
        "files": results
    }

    print(json.dumps(output))
    return 0 if all_valid else 1


def plan_command(base_dir: str, os_filter: Optional[str] = None, module_filter: Optional[list[str]] = None) -> int:
    """Implement the 'plan' command."""
    base_path = Path(base_dir)
    if not base_path.exists():
        print(json.dumps({"error": "directory not found"}), file=sys.stderr)
        return 1

    # Discover configs
    configs = discover_config_files(base_dir)

    if not configs:
        # Check if there were parse errors (no configs but files exist)
        yaml_files = []
        for root, dirs, files in os.walk(base_path):
            dirs[:] = [d for d in dirs if d != '.git']
            for file in files:
                if Path(file).suffix.lower() in ('.yaml', '.yml', '.json'):
                    yaml_files.append(Path(root) / file)

        if yaml_files:
            # There are config files but all have parse errors
            files_list = [{"path": str(f.relative_to(base_path)), "valid": False, "errors": ["parse error: failed to parse"]} for f in yaml_files]
            print(json.dumps({
                "error": "validation_failed",
                "details": {"valid": False, "files": files_list}
            }), file=sys.stderr)
        else:
            print(json.dumps({"error": "no_configs", "details": "no rig config files discovered"}), file=sys.stderr)
        return 1

    # Validate all configs first
    all_valid = True
    validation_results = []
    for rel_path, content in configs:
        errors = validate_config(content, rel_path, base_dir)
        valid = len(errors) == 0
        if not valid:
            all_valid = False

        file_result = {
            "path": rel_path,
            "valid": valid
        }
        if not valid:
            file_result["errors"] = [e.message for e in errors]

        validation_results.append(file_result)

    if not all_valid:
        # Sort by path for consistent output
        validation_results.sort(key=lambda x: x["path"])
        print(json.dumps({
            "error": "validation_failed",
            "details": {"valid": False, "files": validation_results}
        }), file=sys.stderr)
        return 1

    # Check for duplicate module schemas
    module_dirs = {}
    for rel_path, content in configs:
        if content.get('schema') == 'module':
            # Module directory is the parent directory of the config file
            module_dir = Path(rel_path).parent
            module_name = str(module_dir) if module_dir != '.' else Path(rel_path).stem

            # For root-level configs (not in a subdirectory), they're not modules
            if module_dir == '.':
                continue

            if module_name in module_dirs:
                module_dirs[module_name].append(rel_path)
            else:
                module_dirs[module_name] = [rel_path]

    duplicate_modules = {name: paths for name, paths in module_dirs.items() if len(paths) > 1}
    if duplicate_modules:
        for name, paths in duplicate_modules.items():
            print(json.dumps({
                "error": "validation_failed",
                "details": {"valid": False, "files": [{"path": p, "valid": False, "errors": [f"duplicate 'module' schema in '{name}'"]} for p in paths]}
            }), file=sys.stderr)
        return 1

    # Check for missing source files (non-wildcard)
    missing_files = []
    for rel_path, content in configs:
        if content.get('schema') == 'module':
            module_dir = Path(base_dir) / Path(rel_path).parent
            missing = check_source_files_exist(content, module_dir)
            missing_files.extend(missing)

    if missing_files:
        for module_name, filename in missing_files:
            print(json.dumps({
                "error": "file_not_found",
                "details": f"module '{module_name}' missing source file '{filename}'"
            }), file=sys.stderr)
        return 1

    # Generate plan
    modules = []

    for rel_path, content in configs:
        if content.get('schema') != 'module':
            continue

        module_dir = Path(base_dir) / Path(rel_path).parent
        module_name = str(module_dir.relative_to(base_path)) if module_dir != base_path else Path(rel_path).stem

        # Skip root-level configs (not modules)
        if module_dir == base_path:
            continue

        # Apply OS filter
        if os_filter:
            module_os = content.get('os', {}).get('name')
            if module_os and module_os != os_filter:
                continue

        # Apply module filter
        if module_filter and module_name not in module_filter:
            continue

        actions = generate_actions(module_name, content, module_dir, os_filter)

        modules.append({
            'name': module_name,
            'actions': actions
        })

    # Sort modules by name
    modules.sort(key=lambda x: x['name'])

    # Apply module filter intersection
    if module_filter:
        modules = [m for m in modules if m['name'] in module_filter]

    plan = {
        'modules': modules
    }

    print(json.dumps(plan, indent=2))
    return 0


def main():
    parser = argparse.ArgumentParser(description='System Provisioning Planner')
    subparsers = parser.add_subparsers(dest='command', required=True)

    # Validate command
    validate_parser = subparsers.add_parser('validate', help='Validate rig configs')
    validate_parser.add_argument('dir', help='Directory to validate')

    # Plan command
    plan_parser = subparsers.add_parser('plan', help='Generate provisioning plan')
    plan_parser.add_argument('dir', help='Directory to plan')
    plan_parser.add_argument('--os', help='Filter by OS (macos or linux)')
    plan_parser.add_argument('--module', action='append', dest='modules', help='Filter by module name')

    args = parser.parse_args()

    if args.command == 'validate':
        exit_code = validate_command(args.dir)
        sys.exit(exit_code)
    elif args.command == 'plan':
        exit_code = plan_command(args.dir, args.os, args.modules)
        sys.exit(exit_code)


if __name__ == '__main__':
    main()
