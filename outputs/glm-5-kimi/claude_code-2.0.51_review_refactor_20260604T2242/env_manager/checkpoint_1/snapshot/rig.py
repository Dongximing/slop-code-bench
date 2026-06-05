#!/usr/bin/env python3
"""rig.py - System Provisioning Planner MVP (dry-run CLI)."""

import argparse
import json
import os
import sys

import yaml


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def discover_files(base_dir):
    """Recursively discover .yaml/.yml/.json files, skipping .git dirs."""
    results = []
    base_dir = os.path.abspath(base_dir)
    for dirpath, dirnames, filenames in os.walk(base_dir):
        # skip .git directories
        dirnames[:] = [d for d in dirnames if d != '.git']
        for fname in sorted(filenames):
            if fname.endswith(('.yaml', '.yml', '.json')):
                full = os.path.join(dirpath, fname)
                rel = os.path.relpath(full, base_dir)
                results.append((rel, full))
    results.sort(key=lambda t: t[0])
    return results


def parse_file(full_path):
    """Parse a YAML or JSON file. Returns (parsed_data, error_string_or_None)."""
    ext = os.path.splitext(full_path)[1].lower()
    try:
        with open(full_path, 'r') as f:
            if ext == '.json':
                data = json.load(f)
            else:
                data = yaml.safe_load(f)
        return data, None
    except Exception as exc:
        return None, f"parse error: {exc}"


def is_rig_config(data):
    """A file is a rig config only if it has a top-level `version` key."""
    return isinstance(data, dict) and 'version' in data


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_config(data, module_name):
    """
    Validate a rig config dict. Returns list of error strings.
    Errors are generated in the order specified by the spec.
    module_name is the directory-relative path for error messages (may be None).
    """
    errors = []

    # We assume the file has already been determined to be a rig config
    # (has top-level `version` key). But we still validate version.

    # 2. version checks
    version = data.get('version')
    if not isinstance(version, str):
        errors.append("version: must be a string")
    elif version != "1":
        errors.append(f"unsupported version '{version}'")

    # 3. schema checks
    has_schema_key = 'schema' in data
    schema = data.get('schema')
    if not has_schema_key:
        errors.append("schema is required")
    else:
        if not isinstance(schema, str):
            errors.append("schema: must be a string")
        elif schema != "module":
            errors.append(f"unknown schema '{schema}'")

    # 4. os type check
    has_os = 'os' in data
    os_obj = data.get('os')
    if has_os:
        if not isinstance(os_obj, dict):
            errors.append("os: must be an object")

    os_is_dict = isinstance(os_obj, dict)
    os_block = os_obj if os_is_dict else {}

    # 5. os.name
    if has_os and os_is_dict:
        if 'name' not in os_block:
            errors.append("os.name is required")
        else:
            os_name = os_block.get('name')
            if not isinstance(os_name, str):
                errors.append("os.name: must be a string")
            elif os_name not in ('macos', 'linux'):
                errors.append(f"os.name: unsupported value '{os_name}'")

    # 6. os.package_manager
    has_packages = 'packages' in os_block
    has_applications = 'applications' in os_block
    needs_pm = has_packages or has_applications
    pm_present = 'package_manager' in os_block

    if has_os and os_is_dict and needs_pm:
        if not pm_present:
            errors.append("os.package_manager is required when packages or applications are specified")
        else:
            pm = os_block.get('package_manager')
            if not isinstance(pm, str):
                errors.append("os.package_manager: must be a string")
            elif pm not in ('brew', 'apt', 'yum'):
                errors.append(f"os.package_manager: unsupported value '{pm}'")
    elif has_os and os_is_dict and pm_present:
        pm = os_block.get('package_manager')
        if not isinstance(pm, str):
            errors.append("os.package_manager: must be a string")
        elif pm not in ('brew', 'apt', 'yum'):
            errors.append(f"os.package_manager: unsupported value '{pm}'")

    # 7. os.packages
    if has_os and os_is_dict and has_packages:
        pkgs = os_block.get('packages')
        if not isinstance(pkgs, list):
            errors.append("os.packages: must be a list")
        else:
            for i, item in enumerate(pkgs):
                if not isinstance(item, str):
                    errors.append(f"os.packages[{i}]: must be a string")
                elif item == '':
                    errors.append(f"os.packages[{i}]: must not be empty")

    # 8. os.applications
    if has_os and os_is_dict and has_applications:
        apps = os_block.get('applications')
        if not isinstance(apps, list):
            errors.append("os.applications: must be a list")
        else:
            for i, item in enumerate(apps):
                if not isinstance(item, str):
                    errors.append(f"os.applications[{i}]: must be a string")
                elif item == '':
                    errors.append(f"os.applications[{i}]: must not be empty")

    # 9. actions type and item-object checks
    has_actions = 'actions' in data
    actions = data.get('actions')
    if has_actions:
        if not isinstance(actions, list):
            errors.append("actions: must be a list")
        else:
            for i, action in enumerate(actions):
                if not isinstance(action, dict):
                    errors.append(f"actions[{i}]: must be an object")
                    continue
                # 10. actions[N].type
                if 'type' not in action:
                    errors.append(f"actions[{i}].type is required")
                else:
                    atype = action.get('type')
                    if not isinstance(atype, str):
                        errors.append(f"actions[{i}].type: must be a string")
                    elif atype not in ('link', 'copy', 'run'):
                        errors.append(f"actions[{i}].type: unknown type '{atype}'")

                # 11. actions[N].source
                if 'source' not in action:
                    errors.append(f"actions[{i}].source is required")
                else:
                    src = action.get('source')
                    if not isinstance(src, str):
                        errors.append(f"actions[{i}].source: must be a string")
                    elif src == '':
                        errors.append(f"actions[{i}].source: must not be empty")
                    else:
                        # Non-wildcard source must be module-relative and not start with /
                        if src != '*':
                            if src.startswith('/'):
                                errors.append(f"actions[{i}].source: must not start with '/'")

                # 12. actions[N].destination
                action_type = action.get('type') if isinstance(action.get('type'), str) else None
                source_val = action.get('source') if isinstance(action.get('source'), str) else None
                is_wildcard = (source_val == '*')

                if 'destination' in action:
                    dest = action.get('destination')
                    if not isinstance(dest, str):
                        errors.append(f"actions[{i}].destination: must be a string")
                    else:
                        if not dest.startswith('/') and not dest.startswith('~'):
                            errors.append(f"actions[{i}].destination: must start with '/' or '~'")
                        if is_wildcard and not dest.endswith('/'):
                            errors.append(f"actions[{i}].destination: must end with '/' for wildcard source")
                else:
                    # destination not provided
                    if action_type in ('link', 'copy'):
                        errors.append(f"actions[{i}].destination is required for '{action_type}' actions")
                    # For 'run', destination is optional - no error

                # 13. actions[N].hidden and elevated
                if 'hidden' in action:
                    h = action.get('hidden')
                    if not isinstance(h, bool):
                        errors.append(f"actions[{i}].hidden: must be a boolean")
                if 'elevated' in action:
                    e = action.get('elevated')
                    if not isinstance(e, bool):
                        errors.append(f"actions[{i}].elevated: must be a boolean")

    return errors


def validate_all(base_dir):
    """
    Discover and validate all rig configs in base_dir.
    Returns a dict:
      {"valid": bool, "files": [{"path": ..., "valid": bool, "errors": [...]}]}
    """
    files_info = discover_files(base_dir)
    results = []
    all_valid = True

    for rel_path, full_path in files_info:
        data, parse_err = parse_file(full_path)
        file_errors = []

        if parse_err is not None:
            file_errors.append(parse_err)
            # File had a parse error - it's still a rig config candidate
            # since we couldn't determine if it has `version`
            # But per spec: parse errors in discovered files are reported as
            # file-level validation errors.
            # "config discovery errors (parse errors) are reported as file-level
            # validation errors."
            # The file is still discovered (it has the right extension) so we
            # report it. It counts as invalid.
            results.append({
                "path": rel_path,
                "valid": False,
                "errors": file_errors
            })
            all_valid = False
            continue

        # Check if it's a rig config (has top-level version)
        if not is_rig_config(data):
            # Not a rig config - skip entirely
            continue

        # Validate the config
        validation_errors = validate_config(data, None)
        if validation_errors:
            results.append({
                "path": rel_path,
                "valid": False,
                "errors": validation_errors
            })
            all_valid = False
        else:
            results.append({
                "path": rel_path,
                "valid": True
            })

    # Sort results by path
    results.sort(key=lambda x: x['path'])
    return {"valid": all_valid, "files": results}


# ---------------------------------------------------------------------------
# Module detection
# ---------------------------------------------------------------------------

def detect_modules(base_dir, validation_result):
    """
    From validation results, detect modules and check for duplicate schemas.
    Returns (modules_dict, duplicate_errors).
    modules_dict: {module_name: {"files": [file_entries], "dir_abs": abs_path}}
    duplicate_errors: list of error strings
    """
    base_dir = os.path.abspath(base_dir)
    dir_module_configs = {}  # dir_rel -> list of file entries with schema: module

    for file_entry in validation_result['files']:
        if not file_entry['valid']:
            continue
        rel_path = file_entry['path']
        full_path = os.path.join(base_dir, rel_path)
        data, _ = parse_file(full_path)

        # Determine module directory (parent dir relative to base_dir)
        parent = os.path.dirname(rel_path)

        if parent == '':
            # Root-level file - not a module
            continue

        schema = data.get('schema') if isinstance(data, dict) else None
        if schema != 'module':
            continue

        if parent not in dir_module_configs:
            dir_module_configs[parent] = []
        dir_module_configs[parent].append(file_entry)

    # Check for duplicate module schemas
    duplicate_errors = []
    modules = {}
    for dir_rel, entries in dir_module_configs.items():
        module_name = dir_rel.replace(os.sep, '/')
        if len(entries) > 1:
            duplicate_errors.append(
                f"duplicate 'module' schema in '{module_name}'"
            )
        modules[module_name] = {
            "entries": entries,
            "dir_abs": os.path.join(base_dir, dir_rel)
        }

    return modules, duplicate_errors


# ---------------------------------------------------------------------------
# Wildcard expansion
# ---------------------------------------------------------------------------

def expand_wildcard(module_dir_abs, destination_base):
    """
    Expand wildcard source "*" in a module directory.
    Returns list of (source_abs, dest_abs) tuples, sorted by filename.
    """
    results = []
    if not os.path.isdir(module_dir_abs):
        return results

    entries = sorted(os.listdir(module_dir_abs))
    for fname in entries:
        full = os.path.join(module_dir_abs, fname)
        # Exclude directories
        if os.path.isdir(full):
            continue
        # Exclude .yaml, .yml, .json
        if fname.endswith(('.yaml', '.yml', '.json')):
            continue
        # Exclude hidden files
        if fname.startswith('.'):
            continue
        results.append((full, destination_base + fname))

    return results


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def resolve_destination(dest):
    """Resolve ~ in destination using $HOME."""
    home = os.environ.get('HOME', '')
    if dest.startswith('~/'):
        return os.path.join(home, dest[2:])
    elif dest == '~':
        return home
    return dest


def apply_hidden(dest, hidden):
    """If hidden is True, prefix the filename with . unless it already starts with ."""
    if not hidden:
        return dest
    # Get the filename part
    basename = os.path.basename(dest)
    dirname = os.path.dirname(dest)
    if basename.startswith('.'):
        return dest
    return os.path.join(dirname, '.' + basename)


# ---------------------------------------------------------------------------
# Plan generation
# ---------------------------------------------------------------------------

def generate_plan(base_dir, os_filter=None, module_filter=None):
    """
    Generate the full plan. Returns (plan_dict, error_dict_or_None).
    """
    base_dir = os.path.abspath(base_dir)

    # Step 1: Validate all configs
    validation_result = validate_all(base_dir)

    # Step 2: Check for parse errors first (they always produce validation_failed)
    # Then check if any configs exist
    has_any_configs = len(validation_result['files']) > 0

    if not has_any_configs:
        return None, {"error": "no_configs", "details": "no rig config files found"}

    if not validation_result['valid']:
        return None, {"error": "validation_failed", "details": validation_result}

    # Step 3: Detect modules and check duplicates
    modules, dup_errors = detect_modules(base_dir, validation_result)

    if dup_errors:
        # Duplicate module schemas are validation errors
        # Add these errors to the files that have duplicate schemas
        # We need to report this as validation_failed
        # Re-build validation result with duplicate errors
        enhanced_result = {"valid": False, "files": list(validation_result['files'])}
        # Find which files are involved in duplicates
        for file_entry in list(validation_result['files']):
            rel_path = file_entry['path']
            parent = os.path.dirname(rel_path)
            if parent == '':
                continue
            full_path = os.path.join(base_dir, rel_path)
            data, _ = parse_file(full_path)
            if isinstance(data, dict) and data.get('schema') == 'module':
                module_name = parent.replace(os.sep, '/')
                # Check if this module has duplicates
                dir_configs = [f for f in validation_result['files']
                               if os.path.dirname(f['path']) == parent and f['valid']]
                schema_module_configs = []
                for f in dir_configs:
                    fp = os.path.join(base_dir, f['path'])
                    d, _ = parse_file(fp)
                    if isinstance(d, dict) and d.get('schema') == 'module':
                        schema_module_configs.append(f)

                if len(schema_module_configs) > 1:
                    # Add duplicate error to all these files
                    if file_entry.get('valid'):
                        file_entry_copy = dict(file_entry)
                        file_entry_copy['valid'] = False
                        file_entry_copy['errors'] = [
                            f"duplicate 'module' schema in '{module_name}'"
                        ]
                        # Replace in enhanced_result
                        for idx, fe in enumerate(enhanced_result['files']):
                            if fe['path'] == rel_path:
                                enhanced_result['files'][idx] = file_entry_copy
                                break
        return None, {"error": "validation_failed", "details": enhanced_result}

    if not modules:
        return None, {"error": "no_configs", "details": "no valid rig config modules found"}

    # Step 4: Apply filters
    filtered = {}
    for mod_name, mod_data in sorted(modules.items()):
        # Read the module config
        config_path = os.path.join(base_dir, mod_data['entries'][0]['path'])
        config, _ = parse_file(config_path)

        # OS filter
        if os_filter is not None:
            mod_os = config.get('os')
            if mod_os and isinstance(mod_os, dict):
                mod_os_name = mod_os.get('name')
                if mod_os_name is not None and mod_os_name != os_filter:
                    continue

        # Module filter
        if module_filter is not None:
            if mod_name not in module_filter:
                continue

        filtered[mod_name] = (mod_data, config)

    # Build plan
    plan_modules = []
    for mod_name in sorted(filtered.keys()):
        mod_data, config = filtered[mod_name]
        mod_dir_abs = mod_data['dir_abs']
        actions = []

        # install_package actions
        os_obj = config.get('os')
        if os_obj and isinstance(os_obj, dict):
            pm = os_obj.get('package_manager')
            packages = os_obj.get('packages', [])
            if packages and pm:
                for pkg in sorted(packages):
                    actions.append({
                        "type": "install_package",
                        "manager": pm,
                        "package": pkg
                    })

            # install_application actions
            applications = os_obj.get('applications', [])
            if applications and pm:
                for app in sorted(applications):
                    actions.append({
                        "type": "install_application",
                        "manager": pm,
                        "application": app
                    })

        # File actions (in config order)
        config_actions = config.get('actions', [])
        for action_def in config_actions:
            atype = action_def.get('type')
            source = action_def.get('source', '')
            dest = action_def.get('destination')
            hidden = action_def.get('hidden', False)
            elevated = action_def.get('elevated', False)

            if source == '*':
                # Wildcard expansion
                dest_base = resolve_destination(dest) if dest else ''
                expanded = expand_wildcard(mod_dir_abs, dest_base)
                for src_abs, dst_abs in expanded:
                    dst_abs = apply_hidden(dst_abs, hidden)
                    if atype == 'run':
                        actions.append({
                            "type": "run",
                            "source": src_abs,
                            "elevated": elevated
                        })
                    else:
                        actions.append({
                            "type": atype,
                            "source": src_abs,
                            "destination": dst_abs,
                            "elevated": elevated
                        })
            else:
                # Non-wildcard source
                src_abs = os.path.join(mod_dir_abs, source)

                # Check if source file exists
                if not os.path.exists(src_abs):
                    return None, {
                        "error": "file_not_found",
                        "details": {
                            "module": f"module '{mod_name}'",
                            "missing": f"missing source file '{source}'"
                        }
                    }

                if atype == 'run':
                    actions.append({
                        "type": "run",
                        "source": src_abs,
                        "elevated": elevated
                    })
                else:
                    resolved_dest = resolve_destination(dest) if dest else ''
                    resolved_dest = apply_hidden(resolved_dest, hidden)
                    actions.append({
                        "type": atype,
                        "source": src_abs,
                        "destination": resolved_dest,
                        "elevated": elevated
                    })

        plan_modules.append({
            "name": mod_name,
            "actions": actions
        })

    return {"modules": plan_modules}, None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_validate(args):
    base_dir = args.dir
    if not os.path.isdir(base_dir):
        print(json.dumps({"error": "no_configs", "details": f"directory not found: {base_dir}"}),
              file=sys.stderr)
        sys.exit(1)

    result = validate_all(base_dir)
    print(json.dumps(result))
    sys.exit(0 if result['valid'] else 1)


def cmd_plan(args):
    base_dir = args.dir
    if not os.path.isdir(base_dir):
        print(json.dumps({"error": "no_configs", "details": f"directory not found: {base_dir}"}),
              file=sys.stderr)
        sys.exit(1)

    os_filter = args.os_filter if hasattr(args, 'os_filter') else None
    module_filter = args.module if hasattr(args, 'module') and args.module else None

    plan, error = generate_plan(base_dir, os_filter=os_filter, module_filter=module_filter)

    if error:
        print(json.dumps(error), file=sys.stderr)
        sys.exit(1)

    print(json.dumps(plan))
    sys.exit(0)


def main():
    parser = argparse.ArgumentParser(description='System Provisioning Planner')
    subparsers = parser.add_subparsers(dest='command')

    # validate
    validate_parser = subparsers.add_parser('validate')
    validate_parser.add_argument('dir')

    # plan
    plan_parser = subparsers.add_parser('plan')
    plan_parser.add_argument('dir')
    plan_parser.add_argument('--os', dest='os_filter', default=None)
    plan_parser.add_argument('--module', nargs='+', dest='module', default=None)

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
