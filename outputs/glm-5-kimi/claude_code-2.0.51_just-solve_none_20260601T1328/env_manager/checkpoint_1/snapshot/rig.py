#!/usr/bin/env python3
"""System Provisioning Planner MVP - dry-run CLI for rig config validation and planning."""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml


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
        elif schema != "module":
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


def generate_plan(base_dir: Path, os_filter: Optional[str] = None, module_filter: Optional[List[str]] = None) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Generate the provisioning plan.

    Returns:
        Tuple of (plan, error). On success, plan is returned and error is None.
        On failure, plan is None and error is returned.
    """
    # First validate
    validation_result = validate_all_configs(base_dir)

    if not validation_result["valid"]:
        return None, {
            "error": "validation_failed",
            "details": validation_result
        }

    # Collect valid modules
    config_files = discover_config_files(base_dir)
    modules_data = []

    for file_path in config_files:
        data, parse_error = parse_config_file(file_path)

        if parse_error or not is_rig_config(data):
            continue

        if data.get('schema') != "module":
            continue

        module_name = get_module_name(file_path, base_dir)
        if not module_name:
            # Root level, not a module
            continue

        # Check for duplicate module schema
        module_dir_files = []
        for fp, d in [(f, parse_config_file(f)[0]) for f in config_files]:
            if d and d.get('schema') == "module":
                mn = get_module_name(fp, base_dir)
                if mn == module_name:
                    module_dir_files.append(fp)

        if len(module_dir_files) > 1:
            # Duplicate module, already handled in validation
            continue

        modules_data.append((module_name, file_path, data))

    # Check if we have any configs at all
    if not modules_data and not any(
        parse_config_file(f)[1] or (parse_config_file(f)[0] and is_rig_config(parse_config_file(f)[0]))
        for f in config_files
    ):
        # No configs at all (no parse errors, no version files)
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

    for module_name, file_path, data in modules_data:
        # OS filter
        if os_filter is not None:
            os_config = data.get('os')
            if os_config and isinstance(os_config, dict):
                module_os = os_config.get('name')
                if module_os and module_os != os_filter:
                    continue

        # Module name filter
        if module_filter is not None:
            if module_name not in module_filter:
                continue

        filtered_modules.append((module_name, file_path, data))

    # Sort modules alphabetically
    filtered_modules.sort(key=lambda x: x[0])

    # Generate plan
    plan_modules = []

    for module_name, file_path, data in filtered_modules:
        module_dir = file_path.parent
        actions = []

        # Process os config
        os_config = data.get('os')
        if os_config and isinstance(os_config, dict):
            pm = os_config.get('package_manager')

            # Packages
            packages = os_config.get('packages', [])
            if packages:
                for pkg in sorted(packages):
                    actions.append({
                        "type": "install_package",
                        "manager": pm,
                        "package": pkg
                    })

            # Applications
            applications = os_config.get('applications', [])
            if applications:
                for app in sorted(applications):
                    actions.append({
                        "type": "install_application",
                        "manager": pm,
                        "application": app
                    })

        # Process file actions
        config_actions = data.get('actions', [])

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
                    actions.extend(expanded)
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
                    # Apply hidden modifier
                    if destination:
                        dest_abs = resolve_destination(destination)

                        if hidden:
                            # Add . prefix to filename if not already present
                            from pathlib import PurePath
                            pp = PurePath(dest_abs)
                            filename = pp.name
                            if not filename.startswith('.'):
                                dest_abs = str(pp.parent / ('.' + filename))

                        actions.append({
                            "type": action_type,
                            "source": source_abs,
                            "destination": dest_abs,
                            "elevated": elevated
                        })

        plan_modules.append({
            "name": module_name,
            "actions": actions
        })

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

    plan, error = generate_plan(base_dir, os_filter, module_filter)

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
