#!/usr/bin/env python3
"""rig.py - System Provisioning Planner MVP (dry-run CLI)."""

import argparse
import json
import os
import sys

import yaml


# ---------------------------------------------------------------------------
# OS Version Ordering
# ---------------------------------------------------------------------------

OS_VERSIONS = [
    'yosemite', 'el_capitan', 'sierra', 'high_sierra',
    'mojave', 'catalina', 'big_sur', 'monterey', 'ventura', 'sonoma'
]

def get_version_index(version):
    """Get the index of a version in the OS_VERSIONS list. Returns -1 if not found."""
    try:
        return OS_VERSIONS.index(version)
    except ValueError:
        return -1


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

def validate_string_list(items, path):
    """Validate that items is a list of non-empty strings. Returns list of errors."""
    errors = []
    if not isinstance(items, list):
        errors.append(f"{path}: must be a list")
    else:
        for i, item in enumerate(items):
            if not isinstance(item, str):
                errors.append(f"{path}[{i}]: must be a string")
            elif item == '':
                errors.append(f"{path}[{i}]: must not be empty")
    return errors

def validate_config(data):
    """
    Validate a rig config dict. Returns list of error strings.
    Errors are generated in the order specified by the spec.
    """
    errors = []

    # version checks
    version = data.get('version')
    if not isinstance(version, str):
        errors.append("version: must be a string")
    elif version != "1":
        errors.append(f"unsupported version '{version}'")

    # schema checks
    has_schema_key = 'schema' in data
    schema = data.get('schema')
    if not has_schema_key:
        errors.append("schema is required")
    else:
        if not isinstance(schema, str):
            errors.append("schema: must be a string")
        elif schema not in ('module', 'preferences', 'dock'):
            errors.append(f"unrecognized schema '{schema}'")

    # If schema is preferences, validate preferences structure
    if isinstance(schema, str) and schema == 'preferences':
        prefs = data.get('preferences')
        if not isinstance(prefs, list):
            errors.append("preferences: must be a list")
        else:
            for i, pref in enumerate(prefs):
                if not isinstance(pref, dict):
                    errors.append(f"preferences[{i}]: must be an object")
                    continue

                # Check required fields
                if 'name' not in pref:
                    errors.append(f"preferences[{i}].name is required")
                if 'domain' not in pref:
                    errors.append(f"preferences[{i}].domain is required")
                if 'key' not in pref:
                    errors.append(f"preferences[{i}].key is required")
                if 'value' not in pref:
                    errors.append(f"preferences[{i}].value is required")
                if 'value_type' not in pref:
                    errors.append(f"preferences[{i}].value_type is required")
                else:
                    value_type = pref.get('value_type')
                    if not isinstance(value_type, str):
                        errors.append(f"preferences[{i}].value_type: must be a string")
                    elif value_type not in ('bool', 'int', 'string', 'float'):
                        errors.append(f"preferences[{i}].value_type: must be 'bool', 'int', 'string', or 'float', got '{value_type}'")
                if 'apply_command' not in pref:
                    errors.append(f"preferences[{i}].apply_command is required")

                # Check version fields
                if 'min_version' in pref:
                    min_ver = pref.get('min_version')
                    if not isinstance(min_ver, str):
                        errors.append(f"preferences[{i}].min_version: must be a string")
                    elif get_version_index(min_ver) == -1:
                        errors.append(f"preferences[{i}].min_version: unknown version '{min_ver}'")

                if 'max_version' in pref:
                    max_ver = pref.get('max_version')
                    if not isinstance(max_ver, str):
                        errors.append(f"preferences[{i}].max_version: must be a string")
                    elif get_version_index(max_ver) == -1:
                        errors.append(f"preferences[{i}].max_version: unknown version '{max_ver}'")

                # Check min_version <= max_version
                if 'min_version' in pref and 'max_version' in pref:
                    min_idx = get_version_index(pref['min_version'])
                    max_idx = get_version_index(pref['max_version'])
                    if min_idx != -1 and max_idx != -1 and min_idx > max_idx:
                        errors.append(f"preferences[{i}]: min_version '{pref['min_version']}' is later than max_version '{pref['max_version']}'")

        return errors

    # If schema is dock, validate dock structure
    if isinstance(schema, str) and schema == 'dock':
        os_val = data.get('os')
        if not isinstance(os_val, str) or os_val != 'macos':
            errors.append("dock: 'os' is required and must be 'macos'")

        items = data.get('items')
        if not isinstance(items, list):
            errors.append("dock.items: must be a list")
        elif len(items) == 0:
            errors.append("dock.items: must be a non-empty list")
        else:
            for i, item in enumerate(items):
                if not isinstance(item, str):
                    errors.append(f"dock.items[{i}]: must be a string")

        return errors

    # For schema: module, continue with existing validation
    # os type check
    has_os = 'os' in data
    os_obj = data.get('os')
    if has_os:
        if not isinstance(os_obj, dict):
            errors.append("os: must be an object")

    os_is_dict = isinstance(os_obj, dict)
    os_block = os_obj if os_is_dict else {}

    # os.name
    if has_os and os_is_dict:
        if 'name' not in os_block:
            errors.append("os.name is required")
        else:
            os_name = os_block.get('name')
            if not isinstance(os_name, str):
                errors.append("os.name: must be a string")
            elif os_name not in ('macos', 'linux'):
                errors.append(f"os.name: unsupported value '{os_name}'")

    # os.package_manager
    has_packages = 'packages' in os_block
    has_applications = 'applications' in os_block
    needs_pm = has_packages or has_applications
    pm_present = 'package_manager' in os_block

    if has_os and os_is_dict and pm_present:
        pm = os_block.get('package_manager')
        if not isinstance(pm, str):
            errors.append("os.package_manager: must be a string")
        elif pm not in ('brew', 'apt', 'yum'):
            errors.append(f"os.package_manager: unsupported value '{pm}'")

    if has_os and os_is_dict and needs_pm and not pm_present:
        errors.append("os.package_manager is required when packages or applications are specified")

    # os.packages
    if has_os and os_is_dict and has_packages:
        pkgs = os_block.get('packages')
        errors.extend(validate_string_list(pkgs, "os.packages"))

    # os.applications
    if has_os and os_is_dict and has_applications:
        apps = os_block.get('applications')
        errors.extend(validate_string_list(apps, "os.applications"))

    # actions type and item-object checks
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
                # actions[N].type
                if 'type' not in action:
                    errors.append(f"actions[{i}].type is required")
                else:
                    atype = action.get('type')
                    if not isinstance(atype, str):
                        errors.append(f"actions[{i}].type: must be a string")
                    elif atype not in ('link', 'copy', 'run'):
                        errors.append(f"actions[{i}].type: unknown type '{atype}'")

                # actions[N].source
                if 'source' not in action:
                    errors.append(f"actions[{i}].source is required")
                else:
                    src = action.get('source')
                    if not isinstance(src, str):
                        errors.append(f"actions[{i}].source: must be a string")
                    elif src == '':
                        errors.append(f"actions[{i}].source: must not be empty")
                    elif src != '*' and src.startswith('/'):
                        errors.append(f"actions[{i}].source: must not start with '/'")

                # actions[N].destination
                action_type = action.get('type') if isinstance(action.get('type'), str) else None
                source_val = action.get('source') if isinstance(action.get('source'), str) else None
                is_wildcard = (source_val == '*')

                if 'destination' in action:
                    dest = action.get('destination')
                    if not isinstance(dest, str):
                        errors.append(f"actions[{i}].destination: must be a string")
                    elif not dest.startswith('/') and not dest.startswith('~'):
                        errors.append(f"actions[{i}].destination: must start with '/' or '~'")
                    elif is_wildcard and not dest.endswith('/'):
                        errors.append(f"actions[{i}].destination: must end with '/' for wildcard source")
                else:
                    # destination not provided
                    if action_type in ('link', 'copy'):
                        errors.append(f"actions[{i}].destination is required for '{action_type}' actions")
                    # For 'run', destination is optional - no error

                # actions[N].hidden and elevated
                if 'hidden' in action:
                    if not isinstance(action.get('hidden'), bool):
                        errors.append(f"actions[{i}].hidden: must be a boolean")
                if 'elevated' in action:
                    if not isinstance(action.get('elevated'), bool):
                        errors.append(f"actions[{i}].elevated: must be a boolean")

    return errors


def validate_all(base_dir):
    """
    Discover and validate all rig configs in base_dir.
    Returns a dict:
      {"valid": bool, "files": [{"path": ..., "valid": bool, "errors": [...]}]}
    Also checks for multiple dock configs and reports that error.
    """
    files_info = discover_files(base_dir)
    results = []
    all_valid = True
    dock_configs = []  # Track all dock configs found

    for rel_path, full_path in files_info:
        data, parse_err = parse_file(full_path)
        file_errors = []

        if parse_err is not None:
            file_errors.append(parse_err)
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

        # Track dock configs for global validation
        if isinstance(data, dict) and data.get('schema') == 'dock':
            dock_configs.append(rel_path)

        # Validate the config
        validation_errors = validate_config(data)
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

    # Check for multiple dock configs (global validation)
    if len(dock_configs) > 1:
        # Add error to all dock config files
        for dock_path in dock_configs:
            found = False
            for result in results:
                if result['path'] == dock_path:
                    result['valid'] = False
                    result['errors'] = ["multiple 'dock' schemas found — only one is allowed"]
                    found = True
                    break
            if not found:
                results.append({
                    "path": dock_path,
                    "valid": False,
                    "errors": ["multiple 'dock' schemas found — only one is allowed"]
                })
        all_valid = False

    # Sort results by path
    results.sort(key=lambda x: x['path'])
    return {"valid": all_valid, "files": results}


# ---------------------------------------------------------------------------
# Module detection
# ---------------------------------------------------------------------------

def discover_all_modules(base_dir, validation_result):
    """
    Discover all modules (directories with at least one valid rig config).
    This includes modules with schema: module, preferences, or dock.
    Returns dict: {module_name: {"files": [file_entries], "dir_abs": abs_path}}
    """
    base_dir = os.path.abspath(base_dir)
    modules = {}

    for file_entry in validation_result['files']:
        if not file_entry['valid']:
            continue
        rel_path = file_entry['path']

        # Determine module directory (parent dir relative to base_dir)
        parent = os.path.dirname(rel_path)

        if parent == '':
            # Root-level file - not a module
            continue

        module_name = parent.replace(os.sep, '/')

        if module_name not in modules:
            modules[module_name] = {
                "entries": [],
                "dir_abs": os.path.join(base_dir, parent)
            }

        modules[module_name]["entries"].append(file_entry)

    return modules


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
    if dest == '~':
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

def is_preference_in_version_range(pref, os_version):
    """Check if a preference falls within the specified OS version range."""
    if os_version is None:
        return True

    version_idx = get_version_index(os_version)
    if version_idx == -1:
        return False

    min_ver = pref.get('min_version')
    max_ver = pref.get('max_version')

    min_idx = get_version_index(min_ver) if min_ver else 0
    max_idx = get_version_index(max_ver) if max_ver else len(OS_VERSIONS) - 1

    return min_idx <= version_idx <= max_idx


def generate_preference_actions(config, os_version):
    """Generate set_preference actions from a preferences config."""
    actions = []
    prefs = config.get('preferences', [])

    for pref in prefs:
        # Check enabled flag (defaults to True)
        if pref.get('enabled', True) is False:
            continue

        # Check version range
        if not is_preference_in_version_range(pref, os_version):
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

        # Add check_command and expected_state if present
        if 'check_command' in pref:
            action['check_command'] = pref['check_command']
            if 'expected_state' in pref:
                action['expected_state'] = pref['expected_state']

        actions.append(action)

    return actions


def generate_dock_action(config):
    """Generate configure_dock action from a dock config."""
    return {
        "type": "configure_dock",
        "items": config['items']
    }


def detect_conflicts(plan_modules):
    """
    Detect conflicts where multiple link/copy actions target the same destination.
    Returns list of conflict objects sorted by destination.
    """
    # Map: destination -> list of (module, type, source)
    dest_map = {}

    for module in plan_modules:
        mod_name = module['name']
        for action in module['actions']:
            if action['type'] in ('link', 'copy'):
                dest = action['destination']
                if dest not in dest_map:
                    dest_map[dest] = []
                dest_map[dest].append({
                    "module": mod_name,
                    "type": action['type'],
                    "source": action['source']
                })

    # Find conflicts
    conflicts = []
    for dest, sources in dest_map.items():
        if len(sources) > 1:
            # Sort sources by module, then source, then type
            sources_sorted = sorted(sources, key=lambda s: (s['module'], s['source'], s['type']))
            conflicts.append({
                "destination": dest,
                "sources": sources_sorted
            })

    # Sort conflicts by destination
    conflicts.sort(key=lambda c: c['destination'])

    return conflicts


def generate_plan(base_dir, os_filter=None, module_filter=None, os_version=None):
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

    # Check for unknown os_version
    if os_version is not None:
        if get_version_index(os_version) == -1:
            return None, {"error": "unknown_os_version", "details": f"unknown version '{os_version}'"}

    # Step 3: Discover all modules (including preferences and dock)
    all_modules = discover_all_modules(base_dir, validation_result)

    # Step 4: Check for duplicate module schemas
    modules_with_module_schema = {}
    for mod_name, mod_data in all_modules.items():
        for entry in mod_data['entries']:
            full_path = os.path.join(base_dir, entry['path'])
            data, _ = parse_file(full_path)
            if isinstance(data, dict) and data.get('schema') == 'module':
                if mod_name not in modules_with_module_schema:
                    modules_with_module_schema[mod_name] = []
                modules_with_module_schema[mod_name].append(entry)

    # Check for duplicates
    dup_errors = []
    for mod_name, entries in modules_with_module_schema.items():
        if len(entries) > 1:
            dup_errors.append(f"duplicate 'module' schema in '{mod_name}'")

    if dup_errors:
        # Build validation result with duplicate errors
        enhanced_result = {"valid": False, "files": list(validation_result['files'])}
        for file_entry in enhanced_result['files']:
            rel_path = file_entry['path']
            parent = os.path.dirname(rel_path)
            if parent == '':
                continue
            mod_name = parent.replace(os.sep, '/')
            if mod_name in modules_with_module_schema and len(modules_with_module_schema[mod_name]) > 1:
                # Check if this file is one of the duplicates
                full_path = os.path.join(base_dir, rel_path)
                data, _ = parse_file(full_path)
                if isinstance(data, dict) and data.get('schema') == 'module':
                    if file_entry.get('valid'):
                        file_entry['valid'] = False
                        file_entry['errors'] = [f"duplicate 'module' schema in '{mod_name}'"]
        return None, {"error": "validation_failed", "details": enhanced_result}

    if not all_modules:
        return None, {"error": "no_configs", "details": "no valid rig config modules found"}

    # Find dock config (there can be at most one)
    dock_config = None
    dock_module = None
    for mod_name, mod_data in all_modules.items():
        for entry in mod_data['entries']:
            full_path = os.path.join(base_dir, entry['path'])
            data, _ = parse_file(full_path)
            if isinstance(data, dict) and data.get('schema') == 'dock':
                dock_config = data
                dock_module = mod_name
                break
        if dock_config:
            break

    # Step 5: Apply filters and collect configs per module
    filtered = {}
    for mod_name, mod_data in sorted(all_modules.items()):
        # Collect all configs for this module
        configs = []
        for entry in mod_data['entries']:
            full_path = os.path.join(base_dir, entry['path'])
            data, _ = parse_file(full_path)
            if isinstance(data, dict):
                configs.append(data)

        # Check OS filter
        if os_filter is not None:
            # Check if any config in this module specifies an OS
            module_has_os = False
            module_os_matches = False
            for config in configs:
                if config.get('schema') == 'module':
                    mod_os = config.get('os')
                    if mod_os and isinstance(mod_os, dict):
                        module_has_os = True
                        mod_os_name = mod_os.get('name')
                        if mod_os_name == os_filter:
                            module_os_matches = True
                            break

            # If module has an OS specified and it doesn't match, skip
            if module_has_os and not module_os_matches:
                continue

        # Module filter
        if module_filter is not None:
            if mod_name not in module_filter:
                continue

        filtered[mod_name] = (mod_data, configs)

    # Build plan
    plan_modules = []
    for mod_name in sorted(filtered.keys()):
        mod_data, configs = filtered[mod_name]
        mod_dir_abs = mod_data['dir_abs']
        actions = []

        # Find the module config (if any)
        module_config = None
        for config in configs:
            if config.get('schema') == 'module':
                module_config = config
                break

        # Process module config for packages, applications, and file actions
        if module_config:
            # install_package actions
            os_obj = module_config.get('os')
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
            config_actions = module_config.get('actions', [])
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

        # Process preferences configs
        for config in configs:
            if config.get('schema') == 'preferences':
                pref_actions = generate_preference_actions(config, os_version)
                actions.extend(pref_actions)

        # Process dock config (only if this module owns the dock)
        if dock_config and dock_module == mod_name:
            # Check if dock is included by OS filter
            include_dock = True
            if os_filter is not None and os_filter != 'macos':
                include_dock = False
            if include_dock:
                actions.append(generate_dock_action(dock_config))

        plan_modules.append({
            "name": mod_name,
            "actions": actions
        })

    # Step 6: Detect conflicts
    conflicts = detect_conflicts(plan_modules)
    if conflicts:
        return None, {
            "error": "conflict",
            "conflicts": conflicts
        }

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
    os_version = args.os_version if hasattr(args, 'os_version') else None

    plan, error = generate_plan(base_dir, os_filter=os_filter, module_filter=module_filter, os_version=os_version)

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
    plan_parser.add_argument('--os-version', dest='os_version', default=None)

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
