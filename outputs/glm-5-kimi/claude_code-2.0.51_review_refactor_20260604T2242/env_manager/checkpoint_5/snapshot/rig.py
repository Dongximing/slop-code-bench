#!/usr/bin/env python3
"""rig.py - System Provisioning Planner MVP (dry-run CLI)."""

import argparse
import json
import os
import shutil
import stat
import sys
import tarfile

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
    return OS_VERSIONS.index(version) if version in OS_VERSIONS else -1


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def discover_files(base_dir):
    """Recursively discover .yaml/.yml/.json files, skipping .git dirs."""
    results = []
    base_dir = os.path.abspath(base_dir)
    for dirpath, dirnames, filenames in os.walk(base_dir):
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
        elif schema not in ('module', 'preferences', 'dock', 'environments', 'manifest'):
            errors.append(f"unrecognized schema '{schema}'")

    # depends_on validation (can be in any schema type)
    if 'depends_on' in data:
        depends_on = data.get('depends_on')
        if not isinstance(depends_on, list):
            errors.append("depends_on: must be a list")
        else:
            for i, dep in enumerate(depends_on):
                if not isinstance(dep, str):
                    errors.append(f"depends_on[{i}]: must be a string")
                elif dep == '':
                    errors.append(f"depends_on[{i}]: must not be empty")

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

                if 'min_version' in pref and 'max_version' in pref:
                    min_idx = get_version_index(pref['min_version'])
                    max_idx = get_version_index(pref['max_version'])
                    if min_idx != -1 and max_idx != -1 and min_idx > max_idx:
                        errors.append(f"preferences[{i}]: min_version '{pref['min_version']}' is later than max_version '{pref['max_version']}'")

        return errors

    # If schema is environments, validate environments structure
    if isinstance(schema, str) and schema == 'environments':
        envs = data.get('environments')
        if not isinstance(envs, list):
            errors.append("environments: must be a list")
        elif len(envs) == 0:
            errors.append("environments: must be a non-empty list")
        else:
            for i, env in enumerate(envs):
                if not isinstance(env, dict):
                    errors.append(f"environments[{i}]: must be an object")
                    continue

                if 'language' not in env:
                    errors.append(f"environments[{i}].language is required")
                elif not isinstance(env.get('language'), str) or env.get('language') == '':
                    errors.append(f"environments[{i}].language: must be a non-empty string")

                if 'versions' not in env:
                    errors.append(f"environments[{i}].versions is required")
                else:
                    versions = env.get('versions')
                    if not isinstance(versions, list):
                        errors.append(f"environments[{i}].versions: must be a list")
                    elif len(versions) == 0:
                        errors.append(f"environments[{i}].versions: must be a non-empty list")
                    else:
                        for j, ver in enumerate(versions):
                            if not isinstance(ver, str):
                                errors.append(f"environments[{i}].versions[{j}]: must be a string")
                            elif ver == '':
                                errors.append(f"environments[{i}].versions[{j}]: must not be empty")

                if 'manager' not in env:
                    errors.append(f"environments[{i}].manager is required")
                else:
                    manager = env.get('manager')
                    if not isinstance(manager, dict):
                        errors.append(f"environments[{i}].manager: must be an object")
                    else:
                        if 'name' not in manager:
                            errors.append(f"environments[{i}].manager.name is required")
                        elif not isinstance(manager.get('name'), str) or manager.get('name') == '':
                            errors.append(f"environments[{i}].manager.name: must be a non-empty string")

                        if 'plugins' in manager:
                            plugins = manager.get('plugins')
                            if not isinstance(plugins, list):
                                errors.append(f"environments[{i}].manager.plugins: must be a list")
                            else:
                                for k, plugin in enumerate(plugins):
                                    if not isinstance(plugin, dict):
                                        errors.append(f"environments[{i}].manager.plugins[{k}]: must be an object")
                                        continue

                                    if 'name' not in plugin:
                                        errors.append(f"environments[{i}].manager.plugins[{k}].name is required")
                                    elif not isinstance(plugin.get('name'), str) or plugin.get('name') == '':
                                        errors.append(f"environments[{i}].manager.plugins[{k}].name: must be a non-empty string")

                                    if 'virtual_environments' in plugin:
                                        venvs = plugin.get('virtual_environments')
                                        if not isinstance(venvs, list):
                                            errors.append(f"environments[{i}].manager.plugins[{k}].virtual_environments: must be a list")
                                        else:
                                            env_versions = env.get('versions') if isinstance(env.get('versions'), list) else []
                                            for m, venv in enumerate(venvs):
                                                if not isinstance(venv, dict):
                                                    errors.append(f"environments[{i}].manager.plugins[{k}].virtual_environments[{m}]: must be an object")
                                                    continue

                                                if 'version' not in venv:
                                                    errors.append(f"environments[{i}].manager.plugins[{k}].virtual_environments[{m}].version is required")
                                                elif not isinstance(venv.get('version'), str) or venv.get('version') == '':
                                                    errors.append(f"environments[{i}].manager.plugins[{k}].virtual_environments[{m}].version: must be a non-empty string")
                                                elif venv.get('version') not in env_versions:
                                                    errors.append(f"environments[{i}].manager.plugins[{k}].virtual_environments[{m}].version: '{venv.get('version')}' is not in the environment's versions list")

                                                if 'name' not in venv:
                                                    errors.append(f"environments[{i}].manager.plugins[{k}].virtual_environments[{m}].name is required")
                                                elif not isinstance(venv.get('name'), str) or venv.get('name') == '':
                                                    errors.append(f"environments[{i}].manager.plugins[{k}].virtual_environments[{m}].name: must be a non-empty string")

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

    # If schema is manifest, validate manifest structure
    if isinstance(schema, str) and schema == 'manifest':
        profiles = data.get('profiles')
        if not isinstance(profiles, dict):
            errors.append("profiles: must be a non-empty object")
        elif len(profiles) == 0:
            errors.append("profiles: must be a non-empty object")
        else:
            for profile_name, profile_def in profiles.items():
                if not isinstance(profile_def, dict):
                    errors.append(f"profiles.{profile_name}: must be an object")
                    continue

                # modules is required
                if 'modules' not in profile_def:
                    errors.append(f"profiles.{profile_name}.modules is required")
                else:
                    modules = profile_def.get('modules')
                    if not isinstance(modules, list):
                        errors.append(f"profiles.{profile_name}.modules: must be a list")
                    elif len(modules) == 0:
                        errors.append(f"profiles.{profile_name}.modules: must be a non-empty list")
                    else:
                        for i, mod in enumerate(modules):
                            if not isinstance(mod, str):
                                errors.append(f"profiles.{profile_name}.modules[{i}]: must be a string")
                            elif mod == '':
                                errors.append(f"profiles.{profile_name}.modules[{i}]: must not be empty")

                # extends is optional but must reference another profile
                if 'extends' in profile_def:
                    extends = profile_def.get('extends')
                    if not isinstance(extends, str):
                        errors.append(f"profiles.{profile_name}.extends: must be a string")
                    elif extends not in profiles:
                        errors.append(f"profiles.{profile_name}.extends: unknown profile '{extends}'")

        return errors

    # For schema: module, continue with existing validation
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
                if 'type' not in action:
                    errors.append(f"actions[{i}].type is required")
                else:
                    atype = action.get('type')
                    if not isinstance(atype, str):
                        errors.append(f"actions[{i}].type: must be a string")
                    elif atype not in ('link', 'copy', 'run'):
                        errors.append(f"actions[{i}].type: unknown type '{atype}'")

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
    Also checks for manifest schema placement (must be in root, only one allowed).
    """
    files_info = discover_files(base_dir)
    results = []
    all_valid = True
    dock_configs = []
    manifest_configs = []

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

        if not is_rig_config(data):
            continue

        if isinstance(data, dict) and data.get('schema') == 'dock':
            dock_configs.append(rel_path)

        if isinstance(data, dict) and data.get('schema') == 'manifest':
            manifest_configs.append(rel_path)

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

    # Check for manifest placement issues
    if len(manifest_configs) > 1:
        for manifest_path in manifest_configs:
            found = False
            for result in results:
                if result['path'] == manifest_path:
                    result['valid'] = False
                    result['errors'] = ["multiple 'manifest' schemas found — only one is allowed"]
                    found = True
                    break
            if not found:
                results.append({
                    "path": manifest_path,
                    "valid": False,
                    "errors": ["multiple 'manifest' schemas found — only one is allowed"]
                })
        all_valid = False
    elif len(manifest_configs) == 1:
        # Check if manifest is in root (not inside a module directory)
        manifest_path = manifest_configs[0]
        parent = os.path.dirname(manifest_path)
        if parent != '':
            # Manifest is inside a module directory - find the result and update it
            for result in results:
                if result['path'] == manifest_path:
                    result['valid'] = False
                    result['errors'] = [f"manifest schema must be in the root directory, found in '{parent}'"]
                    break
            all_valid = False

    if len(dock_configs) > 1:
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

        parent = os.path.dirname(rel_path)

        if parent == '':
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
# Manifest handling
# ---------------------------------------------------------------------------

def find_manifest(base_dir, validation_result):
    """
    Find and return the manifest config if it exists.
    Returns (manifest_data, manifest_path) or (None, None).
    """
    for file_entry in validation_result['files']:
        if not file_entry.get('valid', False):
            continue
        rel_path = file_entry['path']
        # Check if it's in the root directory (no parent directory)
        parent = os.path.dirname(rel_path)
        if parent != '':
            continue
        full_path = os.path.join(base_dir, rel_path)
        data, _ = parse_file(full_path)
        if isinstance(data, dict) and data.get('schema') == 'manifest':
            return data, rel_path
    return None, None


def resolve_profile_chain(profile_name, manifest, visited=None):
    """
    Resolve the full inheritance chain for a profile.
    Returns (list_of_module_names, cycle_or_None).
    cycle is a list showing the circular dependency.
    """
    if visited is None:
        visited = []

    profiles = manifest.get('profiles', {})
    if profile_name not in profiles:
        return None, None  # Unknown profile (handled separately)

    if profile_name in visited:
        # Found a cycle
        cycle_start = visited.index(profile_name)
        cycle = visited[cycle_start:] + [profile_name]
        return None, cycle

    visited.append(profile_name)
    profile_def = profiles[profile_name]

    modules = []

    # First resolve parent (if any)
    if 'extends' in profile_def:
        parent_name = profile_def['extends']
        parent_modules, cycle = resolve_profile_chain(parent_name, manifest, list(visited))
        if cycle:
            return None, cycle
        if parent_modules is not None:
            modules.extend(parent_modules)

    # Then add own modules
    own_modules = profile_def.get('modules', [])
    if isinstance(own_modules, list):
        modules.extend(own_modules)

    # Deduplicate while preserving order
    seen = set()
    unique_modules = []
    for mod in modules:
        if isinstance(mod, str) and mod not in seen:
            seen.add(mod)
            unique_modules.append(mod)

    return unique_modules, None


def resolve_all_profiles(manifest):
    """
    Resolve all profiles in the manifest.
    Returns (dict_of_resolved_profiles, cycle_or_None).
    """
    profiles = manifest.get('profiles', {})
    resolved = {}

    for profile_name in profiles:
        modules, cycle = resolve_profile_chain(profile_name, manifest)
        if cycle:
            return None, cycle
        resolved[profile_name] = modules

    return resolved, None


def check_invalid_profiles(resolved_profiles, all_modules):
    """
    Check if any resolved profiles reference non-existent modules.
    Returns (invalid_profile_name_or_None, missing_modules_list).
    """
    for profile_name in sorted(resolved_profiles.keys()):
        modules = resolved_profiles[profile_name]
        missing = [m for m in modules if m not in all_modules]
        if missing:
            return profile_name, sorted(missing)
    return None, []


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

def extract_dependencies(configs):
    """
    Extract and merge dependencies from a list of configs.
    Returns a set of unique dependency module names.
    """
    deps = set()
    for config in configs:
        if isinstance(config, dict) and 'depends_on' in config:
            depends_on = config.get('depends_on')
            if isinstance(depends_on, list):
                for dep in depends_on:
                    if isinstance(dep, str) and dep:
                        deps.add(dep)
    return deps


def detect_cycle_and_order(modules_with_deps):
    """
    Detect circular dependencies and return a topologically sorted order.
    modules_with_deps is a dict: {module_name: set_of_dependencies}
    Returns (ordered_list, cycle_list_or_None)
    """
    in_degree = {mod: 0 for mod in modules_with_deps}

    dependents = {mod: set() for mod in modules_with_deps}

    for mod, deps in modules_with_deps.items():
        for dep in deps:
            if dep in modules_with_deps:  # Only count dependencies that exist in included set
                in_degree[mod] += 1
                dependents[dep].add(mod)

    queue = sorted([mod for mod, degree in in_degree.items() if degree == 0])
    result = []

    while queue:
        mod = queue.pop(0)
        result.append(mod)

        for dependent in sorted(dependents[mod]):
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                queue.append(dependent)
                queue.sort()

    if len(result) != len(modules_with_deps):
        cycle = find_cycle(modules_with_deps)
        return None, cycle

    return result, None


def find_cycle(modules_with_deps):
    """
    Find a cycle in the dependency graph using DFS.
    Returns a list representing a cycle, or None if no cycle exists.
    """
    WHITE = 0  # unvisited
    GRAY = 1   # visiting (in current DFS path)
    BLACK = 2  # visited

    color = {mod: WHITE for mod in modules_with_deps}
    parent = {mod: None for mod in modules_with_deps}

    def dfs(mod):
        color[mod] = GRAY
        for dep in sorted(modules_with_deps.get(mod, [])):
            if dep not in modules_with_deps:
                continue  # Skip missing dependencies (handled elsewhere)
            if color[dep] == GRAY:
                # Found a cycle - reconstruct it
                cycle = [dep]
                current = mod
                while current != dep:
                    cycle.append(current)
                    current = parent[current]
                cycle.append(dep)
                cycle.reverse()
                return cycle
            elif color[dep] == WHITE:
                parent[dep] = mod
                result = dfs(dep)
                if result:
                    return result
        color[mod] = BLACK
        return None

    for mod in sorted(modules_with_deps.keys()):
        if color[mod] == WHITE:
            result = dfs(mod)
            if result:
                return result

    return None


def sort_modules_with_profile_order(modules_with_deps, profile_order):
    """
    Topologically sort modules, breaking ties by profile order.
    profile_order is a list of module names in the order they appear in the resolved profile.
    """
    # Create a map for profile order position
    profile_position = {mod: i for i, mod in enumerate(profile_order)}

    # Calculate in-degrees
    in_degree = {mod: 0 for mod in modules_with_deps}
    dependents = {mod: set() for mod in modules_with_deps}

    for mod, deps in modules_with_deps.items():
        for dep in deps:
            if dep in modules_with_deps:
                in_degree[mod] += 1
                dependents[dep].add(mod)

    # Start with modules that have no dependencies
    ready = [mod for mod, degree in in_degree.items() if degree == 0]

    result = []
    while ready:
        # Sort ready modules: first by profile order, then alphabetically
        ready.sort(key=lambda m: (profile_position.get(m, float('inf')), m))
        mod = ready.pop(0)
        result.append(mod)

        for dependent in dependents[mod]:
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                ready.append(dependent)

    return result


# ---------------------------------------------------------------------------
# Environment actions
# ---------------------------------------------------------------------------

def generate_environment_actions(config):
    """
    Generate environment actions from an environments config.
    Returns a list of action dicts in the correct order.
    """
    actions = []
    envs = config.get('environments', [])

    for env in envs:
        language = env.get('language')
        versions = env.get('versions', [])
        manager = env.get('manager', {})
        manager_name = manager.get('name')
        plugins = manager.get('plugins', [])

        # Install runtimes (in version order)
        for version in versions:
            actions.append({
                "type": "install_runtime",
                "language": language,
                "version": version,
                "manager": manager_name
            })

        # Install plugins (in plugin order)
        for plugin in plugins:
            plugin_name = plugin.get('name')
            actions.append({
                "type": "install_plugin",
                "manager": manager_name,
                "plugin": plugin_name
            })

        # Create virtual environments (grouped by plugin)
        for plugin in plugins:
            plugin_name = plugin.get('name')
            venvs = plugin.get('virtual_environments', [])
            for venv in venvs:
                actions.append({
                    "type": "create_virtual_env",
                    "language": language,
                    "manager": manager_name,
                    "plugin": plugin_name,
                    "version": venv.get('version'),
                    "name": venv.get('name')
                })

    return actions


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
        if os.path.isdir(full):
            continue
        if fname.endswith(('.yaml', '.yml', '.json')):
            continue
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
        if pref.get('enabled', True) is False:
            continue

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


def generate_plan(base_dir, os_filter=None, module_filter=None, os_version=None, profile=None):
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

    # Handle profile argument
    manifest, manifest_path = find_manifest(base_dir, validation_result)
    if profile is not None:
        if manifest is None:
            return None, {"error": "no_manifest", "details": "no manifest found but --profile was specified"}

        profiles = manifest.get('profiles', {})
        if profile not in profiles:
            return None, {"error": "unknown_profile", "details": f"unknown profile '{profile}'"}

        # Resolve the profile (including inheritance)
        profile_modules, cycle = resolve_profile_chain(profile, manifest)
        if cycle:
            return None, {"error": "circular_inheritance", "cycle": cycle}

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
        enhanced_result = {"valid": False, "files": list(validation_result['files'])}
        for file_entry in enhanced_result['files']:
            rel_path = file_entry['path']
            parent = os.path.dirname(rel_path)
            if parent == '':
                continue
            mod_name = parent.replace(os.sep, '/')
            if mod_name in modules_with_module_schema and len(modules_with_module_schema[mod_name]) > 1:
                full_path = os.path.join(base_dir, rel_path)
                data, _ = parse_file(full_path)
                if isinstance(data, dict) and data.get('schema') == 'module':
                    if file_entry.get('valid'):
                        file_entry['valid'] = False
                        file_entry['errors'] = [f"duplicate 'module' schema in '{mod_name}'"]
        return None, {"error": "validation_failed", "details": enhanced_result}

    if not all_modules:
        return None, {"error": "no_configs", "details": "no valid rig config modules found"}

    # If profile is specified, check for invalid profile (missing modules)
    if profile is not None:
        invalid_profile, missing_modules = check_invalid_profiles({profile: profile_modules}, all_modules)
        if invalid_profile:
            return None, {
                "error": "invalid_profile",
                "profile": invalid_profile,
                "missing_modules": missing_modules
            }

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
    # If profile is specified, we start with profile_modules as the base filter
    # then apply --module intersection if specified
    effective_module_filter = None
    if profile is not None:
        effective_module_filter = set(profile_modules)
        if module_filter is not None:
            # Intersect with --module names (ignoring unknown module names)
            effective_module_filter = effective_module_filter.intersection(set(module_filter))
    elif module_filter is not None:
        effective_module_filter = set(module_filter)

    # Store profile module order for tie-breaking
    profile_module_order = profile_modules if profile is not None else None

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

        # Module filter (now using effective_module_filter)
        if effective_module_filter is not None:
            if mod_name not in effective_module_filter:
                continue

        filtered[mod_name] = (mod_data, configs)

    # Step 6: Build dependency graph for filtered modules
    modules_with_deps = {}
    for mod_name, (mod_data, configs) in filtered.items():
        deps = extract_dependencies(configs)
        modules_with_deps[mod_name] = deps

    # Auto-include dependencies - keep adding until no new deps are found
    # This happens before we check for missing dependencies
    changed = True
    while changed:
        changed = False
        new_deps = set()
        for mod_name, deps in modules_with_deps.items():
            for dep in deps:
                if dep in all_modules and dep not in filtered:
                    # Add this dependency
                    new_deps.add(dep)
                    changed = True

        for dep_name in new_deps:
            mod_data = all_modules[dep_name]
            configs = []
            for entry in mod_data['entries']:
                full_path = os.path.join(base_dir, entry['path'])
                data, _ = parse_file(full_path)
                if isinstance(data, dict):
                    configs.append(data)

            # Apply OS filter to auto-included dependencies
            if os_filter is not None:
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
                if module_has_os and not module_os_matches:
                    continue

            filtered[dep_name] = (mod_data, configs)
            dep_deps = extract_dependencies(configs)
            modules_with_deps[dep_name] = dep_deps

    # Rebuild modules_with_deps after auto-inclusion
    modules_with_deps = {}
    for mod_name, (mod_data, configs) in filtered.items():
        deps = extract_dependencies(configs)
        modules_with_deps[mod_name] = deps

    # Check for missing dependencies
    for mod_name, deps in modules_with_deps.items():
        for dep in deps:
            if dep not in filtered:
                return None, {
                    "error": "missing_dependency",
                    "module": mod_name,
                    "missing": dep
                }

    # Check for circular dependencies and get ordered list
    ordered_modules, cycle = detect_cycle_and_order(modules_with_deps)
    if cycle:
        return None, {
            "error": "circular_dependency",
            "cycle": cycle
        }

    # When --profile is active, we need to respect profile module order for tie-breaking
    # The current topological sort breaks ties alphabetically
    # We need to re-sort to follow the profile module order for ties
    if profile_module_order is not None:
        # Build a new ordering that respects dependencies but uses profile order for ties
        ordered_modules = sort_modules_with_profile_order(modules_with_deps, profile_module_order)

    # Build plan using the dependency-ordered module list
    plan_modules = []
    file_not_found_errors = []

    for mod_name in ordered_modules:
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
                        file_not_found_errors.append({
                            "module": mod_name,
                            "missing_source": source
                        })
                        continue

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

        # Process environments configs (must come after all other actions)
        for config in configs:
            if config.get('schema') == 'environments':
                env_actions = generate_environment_actions(config)
                actions.extend(env_actions)

        plan_modules.append({
            "name": mod_name,
            "actions": actions
        })

    # Check for file_not_found errors (happens before dependency errors per spec)
    if file_not_found_errors:
        # Return the first error
        first_error = file_not_found_errors[0]
        return None, {
            "error": "file_not_found",
            "details": {
                "module": f"module '{first_error['module']}'",
                "missing": f"missing source file '{first_error['missing_source']}'"
            }
        }

    # Step 7: Detect conflicts
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

    plan, error = generate_plan(
        base_dir,
        os_filter=args.os_filter,
        module_filter=args.module,
        os_version=args.os_version,
        profile=args.profile
    )

    if error:
        print(json.dumps(error), file=sys.stderr)
        sys.exit(1)

    print(json.dumps(plan))
    sys.exit(0)


def cmd_list_profiles(args):
    base_dir = args.dir
    if not os.path.isdir(base_dir):
        print(json.dumps({"error": "no_configs", "details": f"directory not found: {base_dir}"}),
              file=sys.stderr)
        sys.exit(1)

    # Validate all configs first
    validation_result = validate_all(base_dir)

    if not validation_result['valid']:
        print(json.dumps({"error": "validation_failed", "details": validation_result}),
              file=sys.stderr)
        sys.exit(1)

    # Find manifest
    manifest, manifest_path = find_manifest(base_dir, validation_result)

    if manifest is None:
        # No manifest - return empty profiles list
        print(json.dumps({"profiles": []}))
        sys.exit(0)

    # Resolve all profiles
    resolved, cycle = resolve_all_profiles(manifest)
    if cycle:
        print(json.dumps({"error": "circular_inheritance", "cycle": cycle}),
              file=sys.stderr)
        sys.exit(1)

    # Discover all modules for validation
    all_modules = discover_all_modules(base_dir, validation_result)

    # Check for invalid profiles
    invalid_profile, missing_modules = check_invalid_profiles(resolved, all_modules)
    if invalid_profile:
        print(json.dumps({
            "error": "invalid_profile",
            "profile": invalid_profile,
            "missing_modules": missing_modules
        }), file=sys.stderr)
        sys.exit(1)

    # Build output - sorted by profile name, modules sorted alphabetically
    profiles_output = []
    for profile_name in sorted(resolved.keys()):
        modules = sorted(resolved[profile_name])
        profiles_output.append({
            "name": profile_name,
            "modules": modules
        })

    print(json.dumps({"profiles": profiles_output}))
    sys.exit(0)


# ---------------------------------------------------------------------------
# Leaf Profile Resolution
# ---------------------------------------------------------------------------

def find_leaf_profiles(manifest):
    """
    Find all leaf profiles (profiles that no other profile extends).
    Returns a list of profile names sorted alphabetically.
    """
    profiles = manifest.get('profiles', {})
    extended_by = {name: [] for name in profiles}

    for profile_name, profile_def in profiles.items():
        if 'extends' in profile_def:
            parent = profile_def['extends']
            if parent in extended_by:
                extended_by[parent].append(profile_name)

    leaf_profiles = [name for name in profiles if not extended_by[name]]
    return sorted(leaf_profiles)


# ---------------------------------------------------------------------------
# Build Command
# ---------------------------------------------------------------------------

def generate_installer_script(plan, os_name, os_version):
    """
    Generate the installer script content.
    Returns a string containing the bash installer script.
    """
    script_lines = [
        '#!/bin/bash',
        'set -e',
        '',
        '# Generated installer script',
        f'# Target OS: {os_name}' + (f' {os_version}' if os_version else ''),
        '',
        'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
        '',
        'DRY_RUN=false',
        'if [[ "$1" == "--dry-run" ]]; then',
        '    DRY_RUN=true',
        'fi',
        '',
        '# Helper function to create parent directories',
        'ensure_parent_dir() {',
        '    local dest="$1"',
        '    local parent_dir=$(dirname "$dest")',
        '    if [[ ! -d "$parent_dir" ]]; then',
        '        mkdir -p "$parent_dir"',
        '    fi',
        '}',
        '',
        '# Helper to run commands with elevation if needed',
        'run_elevated() {',
        '    local elevated="$1"',
        '    shift',
        '    if [[ "$elevated" == "true" ]]; then',
        '        if command -v sudo &> /dev/null; then',
        '            sudo "$@"',
        '        else',
        '            "$@"',
        '        fi',
        '    else',
        '        "$@"',
        '    fi',
        '}',
        '',
    ]

    # Process modules in plan order
    for module in plan['modules']:
        for action in module['actions']:
            action_type = action['type']

            if action_type == 'install_package':
                manager = action['manager']
                package = action['package']
                script_lines.append(f'if [[ "$DRY_RUN" == "true" ]]; then')
                script_lines.append(f'    echo "[rig] install_package {manager} {package}"')
                script_lines.append(f'else')

                if manager == 'brew':
                    script_lines.append(f'    brew install {package}')
                elif manager == 'apt':
                    script_lines.append(f'    apt-get install -y {package}')
                elif manager == 'yum':
                    script_lines.append(f'    yum install -y {package}')
                else:
                    script_lines.append(f'    echo "Unknown package manager: {manager}"')

                script_lines.append(f'fi')
                script_lines.append('')

            elif action_type == 'install_application':
                manager = action['manager']
                application = action['application']
                script_lines.append(f'if [[ "$DRY_RUN" == "true" ]]; then')
                script_lines.append(f'    echo "[rig] install_application {manager} {application}"')
                script_lines.append(f'else')

                if manager == 'brew':
                    script_lines.append(f'    brew install --cask {application}')
                elif manager == 'apt':
                    script_lines.append(f'    apt-get install -y {application}')
                elif manager == 'yum':
                    script_lines.append(f'    yum install -y {application}')
                else:
                    script_lines.append(f'    echo "Unknown package manager: {manager}"')

                script_lines.append(f'fi')
                script_lines.append('')

            elif action_type == 'link':
                source = action['source']
                dest = action['destination']
                elevated = action.get('elevated', False)

                # Make source relative to SCRIPT_DIR
                source_rel = os.path.basename(source)
                # Find the module part in the path
                for mod in plan['modules']:
                    if mod['name'] in source:
                        module_name = mod['name']
                        source_rel = os.path.join(module_name, os.path.basename(source))
                        break

                script_lines.append(f'if [[ "$DRY_RUN" == "true" ]]; then')
                script_lines.append(f'    echo "[rig] link {source_rel} {dest}"')
                script_lines.append(f'else')
                script_lines.append(f'    ensure_parent_dir "{dest}"')
                script_lines.append(f'    run_elevated "{str(elevated).lower()}" ln -sf "$SCRIPT_DIR/{source_rel}" "{dest}"')
                script_lines.append(f'fi')
                script_lines.append('')

            elif action_type == 'copy':
                source = action['source']
                dest = action['destination']
                elevated = action.get('elevated', False)

                # Make source relative to SCRIPT_DIR
                source_rel = os.path.basename(source)
                for mod in plan['modules']:
                    if mod['name'] in source:
                        module_name = mod['name']
                        source_rel = os.path.join(module_name, os.path.basename(source))
                        break

                script_lines.append(f'if [[ "$DRY_RUN" == "true" ]]; then')
                script_lines.append(f'    echo "[rig] copy {source_rel} {dest}"')
                script_lines.append(f'else')
                script_lines.append(f'    ensure_parent_dir "{dest}"')
                script_lines.append(f'    run_elevated "{str(elevated).lower()}" cp "$SCRIPT_DIR/{source_rel}" "{dest}"')
                script_lines.append(f'fi')
                script_lines.append('')

            elif action_type == 'run':
                source = action['source']
                elevated = action.get('elevated', False)

                # Make source relative to SCRIPT_DIR
                source_rel = os.path.basename(source)
                for mod in plan['modules']:
                    if mod['name'] in source:
                        module_name = mod['name']
                        source_rel = os.path.join(module_name, os.path.basename(source))
                        break

                script_lines.append(f'if [[ "$DRY_RUN" == "true" ]]; then')
                script_lines.append(f'    echo "[rig] run {source_rel}"')
                script_lines.append(f'else')
                script_lines.append(f'    if [[ -x "$SCRIPT_DIR/{source_rel}" ]]; then')
                script_lines.append(f'        run_elevated "{str(elevated).lower()}" "$SCRIPT_DIR/{source_rel}"')
                script_lines.append(f'    else')
                script_lines.append(f'        run_elevated "{str(elevated).lower()}" bash "$SCRIPT_DIR/{source_rel}"')
                script_lines.append(f'    fi')
                script_lines.append(f'fi')
                script_lines.append('')

            elif action_type == 'set_preference':
                name = action['name']
                script_lines.append(f'if [[ "$DRY_RUN" == "true" ]]; then')
                script_lines.append(f'    echo "[rig] set_preference {name}"')
                script_lines.append(f'else')
                if 'check_command' in action and 'expected_state' in action:
                    check_cmd = action['check_command']
                    expected = action['expected_state']
                    apply_cmd = action['apply_command']
                    script_lines.append(f'    current_state=$({check_cmd} 2>/dev/null || echo "")')
                    script_lines.append(f'    if [[ "$current_state" != "{expected}" ]]; then')
                    script_lines.append(f'        {apply_cmd}')
                    script_lines.append(f'    fi')
                else:
                    apply_cmd = action['apply_command']
                    script_lines.append(f'    {apply_cmd}')
                script_lines.append(f'fi')
                script_lines.append('')

            elif action_type == 'configure_dock':
                items = action['items']
                script_lines.append(f'if [[ "$DRY_RUN" == "true" ]]; then')
                script_lines.append(f'    echo "[rig] configure_dock {len(items)} items"')
                script_lines.append(f'else')
                script_lines.append(f'    # Dock configuration for macOS')
                script_lines.append(f'    defaults write com.apple.dock persistent-apps -array')
                for item in items:
                    script_lines.append(f'    defaults write com.apple.dock persistent-apps -array-add \'<dict><key>tile-data</key><dict><key>file-data</key><dict><key>_CFURLString</key><string>{item}</string><key>_CFURLStringType</key><integer>0</integer></dict></dict></dict>\'')
                script_lines.append(f'    killall Dock 2>/dev/null || true')
                script_lines.append(f'fi')
                script_lines.append('')

            elif action_type == 'install_runtime':
                manager = action['manager']
                language = action['language']
                version = action['version']
                script_lines.append(f'if [[ "$DRY_RUN" == "true" ]]; then')
                script_lines.append(f'    echo "[rig] install_runtime {manager} {language} {version}"')
                script_lines.append(f'else')
                if manager == 'pyenv':
                    script_lines.append(f'    pyenv install -s {version}')
                elif manager == 'asdf':
                    script_lines.append(f'    asdf install {language} {version}')
                else:
                    script_lines.append(f'    echo "Unknown runtime manager: {manager}"')
                script_lines.append(f'fi')
                script_lines.append('')

            elif action_type == 'install_plugin':
                manager = action['manager']
                plugin = action['plugin']
                script_lines.append(f'if [[ "$DRY_RUN" == "true" ]]; then')
                script_lines.append(f'    echo "[rig] install_plugin {manager} {plugin}"')
                script_lines.append(f'else')
                if manager == 'pyenv':
                    script_lines.append(f'    PYENV_ROOT="${{PYENV_ROOT:-$HOME/.pyenv}}"')
                    script_lines.append(f'    git clone https://github.com/pyenv/$plugin.git $PYENV_ROOT/plugins/$plugin 2>/dev/null || true')
                elif manager == 'asdf':
                    script_lines.append(f'    asdf plugin add {plugin}')
                else:
                    script_lines.append(f'    echo "Unknown plugin manager: {manager}"')
                script_lines.append(f'fi')
                script_lines.append('')

            elif action_type == 'create_virtual_env':
                manager = action['manager']
                plugin = action['plugin']
                name = action['name']
                version = action.get('version', '')
                script_lines.append(f'if [[ "$DRY_RUN" == "true" ]]; then')
                script_lines.append(f'    echo "[rig] create_virtual_env {manager} {plugin} {name}"')
                script_lines.append(f'else')
                if manager == 'pyenv':
                    script_lines.append(f'    pyenv virtualenv {version} {name} 2>/dev/null || true')
                elif manager == 'asdf':
                    script_lines.append(f'    asdf shell {action.get("language", "python")} {version}')
                    script_lines.append(f'    asdf-venv create {name}')
                else:
                    script_lines.append(f'    echo "Unknown virtual env manager: {manager}"')
                script_lines.append(f'fi')
                script_lines.append('')

    script_lines.append('echo "Installation complete."')

    return '\n'.join(script_lines)


def get_referenced_source_files(plan, base_dir):
    """
    Get all source files referenced by link, copy, and run actions.
    Returns a dict mapping source_abs -> (module_name, relative_path_in_package)
    """
    base_dir = os.path.abspath(base_dir)
    source_files = {}

    for module in plan['modules']:
        mod_name = module['name']
        for action in module['actions']:
            if action['type'] in ('link', 'copy', 'run'):
                source_abs = action['source']
                # The source is already an absolute path from plan generation
                # We need to determine the relative path within the package
                if os.path.exists(source_abs):
                    # Store with module name prefix
                    source_rel = os.path.join(mod_name, os.path.basename(source_abs))
                    source_files[source_abs] = (mod_name, source_rel)

    return source_files


def create_package(base_dir, output_dir, plan, profile, os_name, os_version):
    """
    Create a single package directory.
    Returns the package directory path.
    """
    # Generate package name
    package_name = f"{profile}-package"
    package_dir = os.path.join(output_dir, package_name)

    # Create package directory
    os.makedirs(package_dir, exist_ok=True)

    # Copy referenced source files
    source_files = get_referenced_source_files(plan, base_dir)

    # Track which modules have files
    modules_with_files = set()
    for source_abs, (mod_name, source_rel) in source_files.items():
        # Create module directory in package
        module_dir = os.path.join(package_dir, mod_name)
        os.makedirs(module_dir, exist_ok=True)
        modules_with_files.add(mod_name)

        # Copy source file
        dest_path = os.path.join(package_dir, source_rel)
        shutil.copy2(source_abs, dest_path)

        # Make sure run scripts are executable
        if source_abs.endswith('.sh'):
            st = os.stat(dest_path)
            os.chmod(dest_path, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # Generate installer script
    installer_script = generate_installer_script(plan, os_name, os_version)
    installer_path = os.path.join(package_dir, 'install')
    with open(installer_path, 'w') as f:
        f.write(installer_script)

    # Make installer executable
    st = os.stat(installer_path)
    os.chmod(installer_path, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # Get modules in plan order (filtered to those that exist in plan)
    modules_list = [m['name'] for m in plan['modules']]

    return package_dir, modules_list


def create_archive(package_dir):
    """
    Create a tar.gz archive of the package.
    Returns the archive path.
    """
    archive_path = package_dir + '.archive'

    # Create tar.gz archive
    with tarfile.open(archive_path, 'w:gz') as tar:
        # Add the package directory
        tar.add(package_dir, arcname=os.path.basename(package_dir))

    return archive_path


def cmd_build(args):
    """Build command handler."""
    base_dir = args.dir

    # --os is required for build
    if not args.os_filter:
        print(json.dumps({"error": "missing_flag", "details": "--os is required for build"}),
              file=sys.stderr)
        sys.exit(1)

    if not os.path.isdir(base_dir):
        print(json.dumps({"error": "no_configs", "details": f"directory not found: {base_dir}"}),
              file=sys.stderr)
        sys.exit(1)

    # Determine output directory
    output_dir = args.output
    if output_dir is None:
        output_dir = os.path.join(base_dir, 'build')

    output_dir = os.path.abspath(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    # Validate and find manifest
    validation_result = validate_all(base_dir)

    # Check for any configs
    has_any_configs = len(validation_result['files']) > 0
    if not has_any_configs:
        print(json.dumps({"error": "no_configs", "details": "no rig config files found"}),
              file=sys.stderr)
        sys.exit(1)

    if not validation_result['valid']:
        print(json.dumps({"error": "validation_failed", "details": validation_result}),
              file=sys.stderr)
        sys.exit(1)

    # Check for unknown os_version
    if args.os_version and get_version_index(args.os_version) == -1:
        print(json.dumps({"error": "unknown_os_version", "details": f"unknown version '{args.os_version}'"}),
              file=sys.stderr)
        sys.exit(1)

    # Find manifest
    manifest, manifest_path = find_manifest(base_dir, validation_result)

    # Determine profiles to build
    profiles_to_build = []

    if args.profile:
        # Single profile specified
        if manifest is None:
            print(json.dumps({"error": "no_manifest", "details": "no manifest found but --profile was specified"}),
                  file=sys.stderr)
            sys.exit(1)

        profiles = manifest.get('profiles', {})
        if args.profile not in profiles:
            print(json.dumps({"error": "unknown_profile", "details": f"unknown profile '{args.profile}'"}),
                  file=sys.stderr)
            sys.exit(1)

        # Check for circular inheritance
        profile_modules, cycle = resolve_profile_chain(args.profile, manifest)
        if cycle:
            print(json.dumps({"error": "circular_inheritance", "cycle": cycle}),
                  file=sys.stderr)
            sys.exit(1)

        profiles_to_build = [args.profile]

    elif manifest is not None:
        # Build all leaf profiles
        leaf_profiles = find_leaf_profiles(manifest)

        # Validate each leaf profile and check for issues
        for profile_name in leaf_profiles:
            profile_modules, cycle = resolve_profile_chain(profile_name, manifest)
            if cycle:
                print(json.dumps({"error": "circular_inheritance", "cycle": cycle}),
                      file=sys.stderr)
                sys.exit(1)

        profiles_to_build = leaf_profiles

    else:
        # No manifest and no profile - use default
        profiles_to_build = ['default']

    # Generate packages for each profile
    packages_output = []

    for profile_name in profiles_to_build:
        # Generate plan for this profile
        plan, error = generate_plan(
            base_dir,
            os_filter=args.os_filter,
            os_version=args.os_version,
            profile=profile_name if profile_name != 'default' else None
        )

        if error:
            print(json.dumps(error), file=sys.stderr)
            sys.exit(1)

        # Create package
        package_dir, modules_list = create_package(
            base_dir,
            output_dir,
            plan,
            profile_name,
            args.os_filter,
            args.os_version
        )

        package_entry = {
            "profile": profile_name,
            "os": args.os_filter,
            "directory": package_dir,
            "modules": modules_list
        }

        # Create archive if --release
        if args.release:
            archive_path = create_archive(package_dir)
            package_entry["archive"] = archive_path

        packages_output.append(package_entry)

    # Sort packages by profile name
    packages_output.sort(key=lambda p: p['profile'])

    print(json.dumps({"packages": packages_output}))
    sys.exit(0)


def main():
    parser = argparse.ArgumentParser(description='System Provisioning Planner')
    subparsers = parser.add_subparsers(dest='command')

    validate_parser = subparsers.add_parser('validate')
    validate_parser.add_argument('dir')

    plan_parser = subparsers.add_parser('plan')
    plan_parser.add_argument('dir')
    plan_parser.add_argument('--os', dest='os_filter', default=None)
    plan_parser.add_argument('--module', nargs='+', dest='module', default=None)
    plan_parser.add_argument('--os-version', dest='os_version', default=None)
    plan_parser.add_argument('--profile', dest='profile', default=None)

    list_profiles_parser = subparsers.add_parser('list-profiles')
    list_profiles_parser.add_argument('dir')

    build_parser = subparsers.add_parser('build')
    build_parser.add_argument('dir')
    build_parser.add_argument('--os', dest='os_filter', default=None, required=False)
    build_parser.add_argument('--os-version', dest='os_version', default=None)
    build_parser.add_argument('--profile', dest='profile', default=None)
    build_parser.add_argument('--output', dest='output', default=None)
    build_parser.add_argument('--release', dest='release', action='store_true', default=False)

    args = parser.parse_args()

    commands = {
        'validate': cmd_validate,
        'plan': cmd_plan,
        'list-profiles': cmd_list_profiles,
        'build': cmd_build,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
