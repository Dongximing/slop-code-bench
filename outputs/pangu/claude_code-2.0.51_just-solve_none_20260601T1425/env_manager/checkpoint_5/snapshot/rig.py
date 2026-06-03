#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional
import yaml


class ValidationError(Exception):
    """Raised when a configuration file fails validation."""
    pass


def load_yaml_file(filepath: Path) -> dict:
    """Load and parse a YAML file."""
    try:
        with open(filepath, 'r') as f:
            data = yaml.safe_load(f)
        if data is None:
            raise ValidationError(f"{filepath}: file is empty")
        return data
    except yaml.YAMLError as e:
        raise ValidationError(f"{filepath}: invalid YAML - {e}")


def validate_environments_config(data: dict, filepath: Path) -> dict:
    """Validate environments schema according to specification."""
    schema = data.get('schema', '')
    if schema != 'environments':
        raise ValidationError(f"{filepath}: schema must be 'environments'")

    version = data.get('version', '')
    if version != '1':
        raise ValidationError(f"{filepath}: unsupported version '{version}'")

    environments = data.get('environments')
    if not isinstance(environments, list) or len(environments) == 0:
        raise ValidationError(f"{filepath}: environments must be a non-empty list")

    for i, env in enumerate(environments):
        language = env.get('language', '')
        if not isinstance(language, str) or not language:
            raise ValidationError(f"{filepath}: environments[{i}].language is required and must be a non-empty string")

        versions = env.get('versions')
        if not isinstance(versions, list) or len(versions) == 0:
            raise ValidationError(f"{filepath}: environments[{i}].versions must be a non-empty list")
        for j, v in enumerate(versions):
            if not isinstance(v, str) or not v:
                raise ValidationError(f"{filepath}: environments[{i}].versions[{j}] must be a non-empty string")

        manager = env.get('manager')
        if not isinstance(manager, dict):
            raise ValidationError(f"{filepath}: environments[{i}].manager is required and must be an object")

        manager_name = manager.get('name', '')
        if not isinstance(manager_name, str) or not manager_name:
            raise ValidationError(f"{filepath}: environments[{i}].manager.name is required and must be a non-empty string")

        plugins = manager.get('plugins')
        if plugins is not None:
            if not isinstance(plugins, list):
                raise ValidationError(f"{filepath}: environments[{i}].manager.plugins must be a list")

            for j, plugin in enumerate(plugins):
                plugin_name = plugin.get('name', '')
                if not isinstance(plugin_name, str) or not plugin_name:
                    raise ValidationError(f"{filepath}: environments[{i}].manager.plugins[{j}].name is required and must be a non-empty string")

                venvs = plugin.get('virtual_environments')
                if venvs is not None:
                    if not isinstance(venvs, list):
                        raise ValidationError(f"{filepath}: environments[{i}].manager.plugins[{j}].virtual_environments must be a list")

                    for k, venv in enumerate(venvs):
                        venv_version = venv.get('version', '')
                        venv_name = venv.get('name', '')

                        if not isinstance(venv_version, str) or not venv_version:
                            raise ValidationError(f"{filepath}: environments[{i}].manager.plugins[{j}].virtual_environments[{k}].version is required and must be a non-empty string")

                        if venv_version not in versions:
                            raise ValidationError(f"{filepath}: environments[{i}].manager.plugins[{j}].virtual_environments[{k}].version: '{venv_version}' is not in the environment's versions list")

                        if not isinstance(venv_name, str) or not venv_name:
                            raise ValidationError(f"{filepath}: environments[{i}].manager.plugins[{j}].virtual_environments[{k}].name is required and must be a non-empty string")

    return data


def validate_module_config(data: dict, filepath: Path) -> dict:
    """Validate module schema."""
    schema = data.get('schema', '')
    if schema != 'module':
        raise ValidationError(f"{filepath}: schema must be 'module'")

    version = data.get('version', '')
    if version != '1':
        raise ValidationError(f"{filepath}: unsupported version '{version}'")

    depends_on = data.get('depends_on')
    if depends_on is not None:
        if not isinstance(depends_on, list):
            raise ValidationError(f"{filepath}: depends_on must be a list")
        for i, dep in enumerate(depends_on):
            if not isinstance(dep, str) or not dep:
                raise ValidationError(f"{filepath}: depends_on[{i}] must be a non-empty string")

    return data


def validate_preferences_config(data: dict, filepath: Path) -> dict:
    """Validate preferences schema."""
    schema = data.get('schema', '')
    if schema != 'preferences':
        raise ValidationError(f"{filepath}: schema must be 'preferences'")

    version = data.get('version', '')
    if version != '1':
        raise ValidationError(f"{filepath}: unsupported version '{version}'")

    return data


def validate_dock_config(data: dict, filepath: Path) -> dict:
    """Validate dock schema."""
    schema = data.get('schema', '')
    if schema != 'dock':
        raise ValidationError(f"{filepath}: schema must be 'dock'")

    version = data.get('version', '')
    if version != '1':
        raise ValidationError(f"{filepath}: unsupported version '{version}'")

    return data


def validate_manifest_config(data: dict, filepath: Path, dotfiles_dir: Path, manifest_found: list) -> dict:
    """Validate manifest schema."""
    schema = data.get('schema', '')
    if schema != 'manifest':
        raise ValidationError(f"{filepath}: schema must be 'manifest'")

    version = data.get('version', '')
    if version != '1':
        raise ValidationError(f"{filepath}: unsupported version '{version}'")

    if filepath.parent != dotfiles_dir:
        module_name = filepath.parent.name
        raise ValidationError(f"manifest schema must be in the root directory, found in '{module_name}'")

    manifest_found.append(filepath)

    profiles = data.get('profiles')
    if not isinstance(profiles, dict) or len(profiles) == 0:
        raise ValidationError(f"{filepath}: profiles must be a non-empty object")

    # Collect profile names for extends validation
    profile_names = set(profiles.keys())

    for profile_name, profile_def in profiles.items():
        if not isinstance(profile_def, dict):
            raise ValidationError(f"{filepath}: profiles.{profile_name} must be an object")

        modules = profile_def.get('modules')
        if not isinstance(modules, list) or len(modules) == 0:
            raise ValidationError(f"{filepath}: profiles.{profile_name}.modules must be a non-empty list")

        for i, module_name in enumerate(modules):
            if not isinstance(module_name, str) or not module_name:
                raise ValidationError(f"{filepath}: profiles.{profile_name}.modules[{i}] must be a non-empty string")

        extends = profile_def.get('extends')
        if extends is not None:
            if not isinstance(extends, str) or not extends:
                raise ValidationError(f"{filepath}: profiles.{profile_name}.extends must be a non-empty string")
            if extends not in profile_names:
                raise ValidationError(f"{filepath}: profiles.{profile_name}.extends: unknown profile '{extends}'")

    return data


def parse_config_file(filepath: Path, dotfiles_dir: Path, manifest_found: list) -> tuple[str, Optional[dict], Optional[dict], Optional[dict], Optional[dict]]:
    """Parse a config file and return (module_name, module_data, environments_data, preferences_data, dock_data)."""
    data = load_yaml_file(filepath)
    schema = data.get('schema', '')

    if schema == 'environments':
        validate_environments_config(data, filepath)
        return filepath.parent.name, None, data, None, None
    elif schema == 'module':
        validate_module_config(data, filepath)
        return filepath.parent.name, data, None, None, None
    elif schema == 'preferences':
        validate_preferences_config(data, filepath)
        return filepath.parent.name, None, None, data, None
    elif schema == 'dock':
        validate_dock_config(data, filepath)
        return filepath.parent.name, None, None, None, data
    elif schema == 'manifest':
        validate_manifest_config(data, filepath, dotfiles_dir, manifest_found)
        # Return "manifest" as a special module to track it, but it's not really a module
        return "manifest", data, None, None, None
    else:
        raise ValidationError(f"{filepath}: invalid schema '{schema}'")


def discover_modules(dotfiles_dir: Path) -> dict:
    """Discover all modules and manifest in the dotfiles directory."""
    modules = {}
    manifest_found = []

    for config_file in dotfiles_dir.rglob('*.yaml'):
        try:
            module_name, module_data, environments_data, preferences_data, dock_data = parse_config_file(config_file, dotfiles_dir, manifest_found)

            if module_name not in modules:
                modules[module_name] = {
                    'name': module_name,
                    'module_data': None,
                    'environments_data': None,
                    'preferences_data': None,
                    'dock_data': None,
                }

            if module_data is not None:
                existing = modules[module_name]['module_data']
                if existing is None:
                    modules[module_name]['module_data'] = module_data
                else:
                    existing_depends = set(existing.get('depends_on', []))
                    new_depends = set(module_data.get('depends_on', []))
                    merged = existing_depends | new_depends
                    existing['depends_on'] = list(merged)

            if environments_data is not None:
                modules[module_name]['environments_data'] = environments_data

            if preferences_data is not None:
                modules[module_name]['preferences_data'] = preferences_data

            if dock_data is not None:
                modules[module_name]['dock_data'] = dock_data

        except ValidationError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    # Check for multiple manifest schemas
    if len(manifest_found) > 1:
        print(f"Error: multiple 'manifest' schemas found — only one is allowed", file=sys.stderr)
        sys.exit(1)

    # Extract manifest data from modules
    manifest_data = None
    if manifest_found:
        # Find the manifest entry that has module_data (parsed yaml)
        for name, module in modules.items():
            if name == "manifest" and module.get('module_data'):
                manifest_data = module['module_data']
                # Remove from modules dict since it's not a real module
                del modules[name]
                break

    return modules, manifest_data


def build_dependency_graph(modules: dict[str, dict]) -> dict[str, list[str]]:
    """Build a dependency graph from module data."""
    graph = {}

    for name, module in modules.items():
        depends_on = module['module_data'].get('depends_on', []) if module['module_data'] else []
        # Deduplicate and ensure it's a list
        graph[name] = list(set(depends_on))

    return graph


def detect_cycle_dfs(node, graph, visited, recursion_stack, cycle_stack):
    """DFS helper for cycle detection."""
    visited.add(node)
    recursion_stack.add(node)
    cycle_stack.append(node)

    for neighbor in graph.get(node, []):
        if neighbor not in visited:
            result = detect_cycle_dfs(neighbor, graph, visited, recursion_stack, cycle_stack)
            if result:
                return result
        elif neighbor in recursion_stack:
            start_idx = cycle_stack.index(neighbor)
            return cycle_stack[start_idx:] + [neighbor]

    cycle_stack.pop()
    recursion_stack.remove(node)
    return None


def resolve_profile(manifest_data: dict, profile_name: str) -> list[str]:
    """Resolve a profile's modules, handling inheritance and deduplication."""
    profiles = manifest_data.get('profiles', {})

    def resolve_with_inheritance(name: str, visited: set) -> list[str]:
        """Recursively resolve modules through inheritance chain."""
        if name in visited:
            return None

        profile_def = profiles.get(name)
        if not profile_def:
            return None

        visited.add(name)
        inherited_modules = []

        extends = profile_def.get('extends')
        if extends:
            parent_modules = resolve_with_inheritance(extends, visited)
            if parent_modules is None:
                return None
            inherited_modules = parent_modules

        own_modules = profile_def.get('modules', [])
        result = inherited_modules + own_modules

        return result

    visited = set()
    modules = resolve_with_inheritance(profile_name, visited)
    if modules is None:
        return None

    seen = set()
    deduped = []
    for m in modules:
        if m not in seen:
            seen.add(m)
            deduped.append(m)

    return deduped


def detect_profile_cycle(manifest_data: dict) -> Optional[list[str]]:
    """Detect circular inheritance in profile definitions."""
    profiles = manifest_data.get('profiles', {})

    def dfs(name: str, visited: set, recursion_stack: set, path: list) -> Optional[list[str]]:
        if name in recursion_stack:
            start_idx = path.index(name)
            return path[start_idx:] + [name]

        if name in visited:
            return None

        visited.add(name)
        recursion_stack.add(name)
        path.append(name)

        profile_def = profiles.get(name)
        if profile_def:
            extends = profile_def.get('extends')
            if extends:
                result = dfs(extends, visited, recursion_stack, path)
                if result:
                    return result

        path.pop()
        recursion_stack.remove(name)
        return None

    visited = set()
    recursion_stack = set()

    for profile_name in profiles:
        if profile_name not in visited:
            cycle = dfs(profile_name, visited, recursion_stack, [])
            if cycle:
                return cycle

    return None


def get_missing_modules(manifest_data: dict, profile_name: str, available_modules: set) -> list[str]:
    """Get list of modules referenced by a resolved profile that don't exist."""
    resolved = resolve_profile(manifest_data, profile_name)
    if resolved is None:
        return None

    missing = [m for m in resolved if m not in available_modules]
    return sorted(missing) if missing else []


def resolve_all_profiles(manifest_data: dict) -> dict[str, list[str]]:
    """Resolve all profiles and return a map of profile names to resolved modules."""
    profiles = manifest_data.get('profiles', {})
    resolved = {}

    for profile_name in sorted(profiles.keys()):
        modules = resolve_profile(manifest_data, profile_name)
        if modules:
            resolved[profile_name] = modules

    return resolved


def detect_cycle(graph: dict[str, list[str]]) -> Optional[list[str]]:
    """Detect a cycle in the dependency graph."""
    visited = set()
    recursion_stack = set()
    cycle_stack = []

    for node in graph:
        if node not in visited:
            result = detect_cycle_dfs(node, graph, visited, recursion_stack, cycle_stack)
            if result:
                return result

    return None


def topological_sort(graph: dict[str, list[str]], modules: dict[str, dict], include_modules: Optional[list[str]] = None) -> list[str]:
    """Perform topological sort on the dependency graph with optional module filtering."""
    cycle = detect_cycle(graph)
    if cycle:
        print(json.dumps({"error": "circular_dependency", "cycle": cycle}), file=sys.stderr)
        sys.exit(1)

    # If include_modules is specified, build subgraph with transitive dependencies
    if include_modules is not None:
        # Validate all include_modules exist
        available_modules = set(modules.keys())
        # Filter out unknown module names
        start_modules = [m for m in include_modules if m in available_modules]

        # Build subgraph with transitive dependencies
        # First, collect all relevant modules (those in start_modules + their dependencies)
        relevant = set()
        queue = list(start_modules)
        while queue:
            node = queue.pop(0)
            if node in relevant or node not in graph:
                continue
            relevant.add(node)

            # Add all dependencies
            for dep in graph.get(node, []):
                if dep not in relevant:
                    queue.append(dep)

        # Build subgraph with only relevant modules
        subgraph = {}
        for node in relevant:
            if node in graph:
                subgraph[node] = [d for d in graph[node] if d in relevant]

        # Standard topological sort on subgraph
        in_degree = {node: 0 for node in subgraph}
        for node, deps in subgraph.items():
            for dep in deps:
                if dep in in_degree:
                    in_degree[dep] = in_degree.get(dep, 0) + 1

        queue = [node for node in in_degree if in_degree[node] == 0]
        queue.sort()

        result = []
        while queue:
            node = queue.pop(0)
            result.append(node)

            for neighbor in subgraph.get(node, []):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

            queue.sort()

        return result

    # Original logic when no filtering
    in_degree = {node: 0 for node in graph}
    for node, deps in graph.items():
        for dep in deps:
            if dep in in_degree:
                in_degree[dep] = in_degree.get(dep, 0) + 1
            else:
                in_degree[dep] = 1

    queue = [node for node in in_degree if in_degree[node] == 0]
    queue.sort()

    result = []
    while queue:
        node = queue.pop(0)
        result.append(node)

        for neighbor in graph.get(node, []):
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

        queue.sort()

    return result


def validate_dependencies_exist(graph: dict[str, list[str]], modules: dict[str, dict]):
    """Check that all dependencies reference existing modules."""
    all_modules = set(graph.keys())

    for module_name, deps in graph.items():
        for dep in deps:
            if dep not in all_modules:
                print(json.dumps({
                    "error": "missing_dependency",
                    "module": module_name,
                    "missing": dep
                }), file=sys.stderr)
                sys.exit(1)


def generate_environment_actions(module_name: str, environments_data: dict) -> list[dict]:
    """Generate environment actions from environments config."""
    actions = []
    environments = environments_data.get('environments', [])

    for env in environments:
        language = env['language']
        versions = env['versions']
        manager = env['manager']['name']
        plugins = env['manager'].get('plugins', [])

        for version in versions:
            actions.append({
                "type": "install_runtime",
                "language": language,
                "version": version,
                "manager": manager
            })

        for plugin in plugins:
            plugin_name = plugin['name']
            actions.append({
                "type": "install_plugin",
                "manager": manager,
                "plugin": plugin_name
            })

        for plugin in plugins:
            plugin_name = plugin['name']
            virtual_envs = plugin.get('virtual_environments', [])
            for venv in virtual_envs:
                actions.append({
                    "type": "create_virtual_env",
                    "language": language,
                    "manager": manager,
                    "plugin": plugin_name,
                    "version": venv['version'],
                    "name": venv['name']
                })

    return actions


def filter_modules(modules: dict[str, dict], os_filter: Optional[str], module_filter: Optional[str]) -> dict[str, dict]:
    """Filter modules based on OS and module filters."""
    filtered = {}

    for name, module in modules.items():
        if module_filter and name != module_filter:
            continue

        module_data = module['module_data']
        if module_data and 'os' in module_data:
            os_config = module_data['os']
            if os_filter and os_config.get('name') != os_filter:
                continue

        filtered[name] = module

    return filtered


def plan_module_actions(module: dict[str, Any], dotfiles_dir: Path) -> list[dict]:
    """Generate actions for a single module."""
    actions = []
    module_name = module['name']
    module_data = module['module_data']
    environments_data = module['environments_data']

    if module_data:
        os_config = module_data.get('os', {})
        packages = os_config.get('packages', [])
        package_manager = os_config.get('package_manager', '')

        for package in sorted(packages):
            actions.append({
                "type": "install_package",
                "manager": package_manager,
                "package": package
            })

        applications = module_data.get('applications', [])
        for app in sorted(applications):
            actions.append({
                "type": "install_application",
                "application": app
            })

        file_actions = module_data.get('actions', [])
        for action in file_actions:
            action_type = action.get('type')
            if action_type in ('link', 'copy', 'run'):
                new_action = dict(action)
                if 'source' in action:
                    source = action['source']
                    if not source.startswith('/'):
                        new_action['source'] = str(dotfiles_dir / module_name / source)
                    if 'elevated' in action:
                        new_action['elevated'] = action['elevated']
                actions.append(new_action)

        preferences = module_data.get('preferences', [])
        for pref in preferences:
            actions.append({
                "type": "set_preference",
                **pref
            })

        if 'dock' in module_data:
            actions.append({"type": "configure_dock"})

    if environments_data:
        env_actions = generate_environment_actions(module_name, environments_data)
        actions.extend(env_actions)

    return actions


def run_list_profiles(dotfiles_dir: Path) -> dict:
    """List all profiles with their resolved modules."""
    modules, manifest = discover_modules(dotfiles_dir)

    modules = {k: v for k, v in modules.items() if k != "manifest"}

    if not manifest:
        return {"profiles": []}

    manifest_data = manifest.get('module_data', manifest)

    cycle = detect_profile_cycle(manifest_data)
    if cycle:
        print(json.dumps({
            "error": "circular_inheritance",
            "cycle": cycle
        }), file=sys.stderr)
        sys.exit(1)

    available_modules = set(modules.keys())
    profiles = manifest_data.get('profiles', {})
    resolved_profiles = []

    for profile_name in sorted(profiles.keys()):
        resolved = resolve_profile(manifest_data, profile_name)
        if resolved:
            # Check for missing modules
            missing = get_missing_modules(manifest_data, profile_name, available_modules)
            if missing:
                print(json.dumps({
                    "error": "invalid_profile",
                    "profile": profile_name,
                    "missing_modules": missing
                }), file=sys.stderr)
                sys.exit(1)
            resolved_profiles.append({
                "name": profile_name,
                "modules": sorted(resolved)
            })

    # Sort profiles alphabetically by name
    resolved_profiles.sort(key=lambda x: x['name'])

    return {"profiles": resolved_profiles}


def run_plan(dotfiles_dir: Path, os_filter: Optional[str] = None, module_filter: Optional[str] = None, profile_filter: Optional[str] = None) -> dict:
    """Generate the plan for the given dotfiles directory."""
    modules, manifest = discover_modules(dotfiles_dir)

    # Filter out manifest from modules list (it's not a real module)
    modules = {k: v for k, v in modules.items() if k != "manifest"}

    if not modules:
        return {"modules": []}

    if profile_filter:
        if not manifest:
            print(json.dumps({
                "error": "no_manifest",
                "details": "no manifest found but --profile was specified"
            }), file=sys.stderr)
            sys.exit(1)

        manifest_data = manifest
        profiles = manifest_data.get('profiles', {})

        cycle = detect_profile_cycle(manifest_data)
        if cycle:
            print(json.dumps({
                "error": "circular_inheritance",
                "cycle": cycle
            }), file=sys.stderr)
            sys.exit(1)

        if profile_filter not in profiles:
            print(json.dumps({
                "error": "unknown_profile",
                "details": f"unknown profile '{profile_filter}'"
            }), file=sys.stderr)
            sys.exit(1)

        resolved_modules = resolve_profile(manifest_data, profile_filter)
        if resolved_modules is None:
            resolved_modules = profiles.get(profile_filter, {}).get('modules', [])

        available_modules = set(modules.keys())
        missing = get_missing_modules(manifest_data, profile_filter, available_modules)
        if missing:
            print(json.dumps({
                "error": "invalid_profile",
                "profile": profile_filter,
                "missing_modules": missing
            }), file=sys.stderr)
            sys.exit(1)

        existing_resolved = [m for m in resolved_modules if m in modules]

        relevant = set()
        queue = list(existing_resolved)
        while queue:
            node = queue.pop(0)
            if node in relevant or node not in modules:
                continue
            relevant.add(node)

            module_data = modules[node].get('module_data', {})
            if module_data:
                for dep in module_data.get('depends_on', []):
                    if dep not in relevant:
                        queue.append(dep)

        if module_filter:
            if module_filter in relevant:
                temp_relevant = set()
                queue = [module_filter]
                while queue:
                    node = queue.pop(0)
                    if node in temp_relevant or node not in relevant:
                        continue
                    temp_relevant.add(node)

                    module_data = modules[node].get('module_data', {})
                    if module_data:
                        for dep in module_data.get('depends_on', []):
                            if dep in relevant and dep not in temp_relevant:
                                queue.append(dep)
                relevant = temp_relevant
            else:
                relevant = set()

        graph = {}
        for node in relevant:
            if node in modules:
                module_data = modules[node].get('module_data', {})
                depends_on = module_data.get('depends_on', []) if module_data else []
                graph[node] = list(set(depends_on))

        validate_dependencies_exist(graph, modules)
        ordered_names = topological_sort(graph, modules)

    else:
        filtered_modules = filter_modules(modules, os_filter, module_filter)

        if not filtered_modules:
            return {"modules": []}

        graph = build_dependency_graph(filtered_modules)
        validate_dependencies_exist(graph, filtered_modules)
        ordered_names = topological_sort(graph, filtered_modules)
        return {"modules": plan_modules_from_ordered(ordered_names, filtered_modules, dotfiles_dir)}

    plan_modules = []
    for name in ordered_names:
        module = modules[name]
        actions = plan_module_actions(module, dotfiles_dir)
        plan_modules.append({
            "name": name,
            "actions": actions
        })

    return {"modules": plan_modules}


def plan_modules_from_ordered(ordered_names: list[str], modules: dict[str, dict], dotfiles_dir: Path) -> list[dict]:
    """Generate plan_modules list from ordered names."""
    plan_modules = []
    for name in ordered_names:
        module = modules[name]
        actions = plan_module_actions(module, dotfiles_dir)
        plan_modules.append({
            "name": name,
            "actions": actions
        })
    return plan_modules


def get_leaf_profiles(manifest_data: dict) -> list[str]:
    """Get leaf profiles - profiles that no other profile extends."""
    profiles = manifest_data.get('profiles', {})
    all_profiles = set(profiles.keys())
    extended_profiles = set()

    for profile_def in profiles.values():
        extends = profile_def.get('extends')
        if extends:
            extended_profiles.add(extends)

    leaf_profiles = sorted(all_profiles - extended_profiles)
    return leaf_profiles


def get_referenced_source_files(module: dict, dotfiles_dir: Path) -> list[tuple[Path, Path]]:
    """Get all source files referenced by a module's link, copy, and run actions.

    Returns list of (source_path, relative_dest_path) tuples.
    """
    referenced = []
    module_name = module['name']
    module_dir = dotfiles_dir / module_name

    actions = module.get('actions', [])
    for action in actions:
        action_type = action.get('type')
        if action_type in ('link', 'copy', 'run'):
            source = action.get('source')
            if source:
                source_path = Path(source)
                # Resolve relative to module directory
                if not source_path.is_absolute():
                    full_source = module_dir / source_path
                else:
                    full_source = source_path

                # For link/copy, get the destination path to preserve directory structure
                if action_type in ('link', 'copy'):
                    dest = action.get('dest', '')
                    if dest:
                        # Use the source filename as the relative path within module
                        referenced.append((full_source, source_path))
                else:  # run action
                    referenced.append((full_source, source_path))

    return referenced


def prepare_plan_for_installer(plan: dict, dotfiles_dir: Path) -> dict:
    """Prepare the plan for installer generation by converting absolute source paths to relative.

    Returns a modified plan with relative source paths.
    """
    import copy
    modified_plan = copy.deepcopy(plan)

    for module in modified_plan['modules']:
        module_name = module['name']

        for action in module['actions']:
            action_type = action.get('type')

            if action_type in ('link', 'copy', 'run'):
                source = action.get('source')
                if source:
                    source_path = Path(source)

                    # Convert to relative path if it's an absolute path from dotfiles
                    if source_path.is_absolute():
                        try:
                            # Try to make it relative to dotfiles_dir
                            relative_source = source_path.relative_to(dotfiles_dir)
                            action['source'] = str(relative_source)
                        except ValueError:
                            # Not under dotfiles_dir, keep as is
                            pass
                    else:
                        # Already relative, but needs module prefix for package
                        action['source'] = f"{module_name}/{source}"

    return modified_plan


def copy_source_files(plan: dict, dotfiles_dir: Path, package_dir: Path) -> list[str]:
    """Copy all referenced source files to the package directory.

    Returns list of module names that had files copied.
    """
    modules_with_files = []

    for module in plan['modules']:
        module_name = module['name']
        files_copied = False

        for action in module['actions']:
            action_type = action.get('type')
            if action_type in ('link', 'copy', 'run'):
                source = action.get('source')
                if source:
                    source_path = Path(source)

                    # Resolve source path - it could be relative to module or absolute
                    if not source_path.is_absolute():
                        # Check if it already has module prefix (from prepare_plan_for_installer)
                        parts = source_path.parts
                        if len(parts) > 1 and parts[0] == module_name:
                            # Source is "module_name/relative_path" - under dotfiles
                            full_source = dotfiles_dir / source_path
                        else:
                            # Source is just "relative_path" - directly under module
                            full_source = dotfiles_dir / module_name / source_path
                    else:
                        full_source = source_path

                    # Destination in package: module_name / relative_path
                    if source_path.is_absolute():
                        try:
                            relative_to_dotfiles = source_path.relative_to(dotfiles_dir)
                            # Save path within package preserving module structure
                            dest_in_package = package_dir / relative_to_dotfiles
                        except ValueError:
                            # Not under dotfiles, save by module/filename
                            dest_in_package = package_dir / module_name / source_path.name
                    else:
                        # For relative paths, preserve the structure
                        dest_in_package = package_dir / source_path

                    dest_in_package.parent.mkdir(parents=True, exist_ok=True)

                    if full_source.exists():
                        import shutil
                        shutil.copy2(full_source, dest_in_package)
                        files_copied = True

        if files_copied:
            modules_with_files.append(module_name)

    return modules_with_files


def generate_installer_script(plan: dict, os_name: str, os_version: Optional[str]) -> str:
    """Generate the installer bash script."""
    script = '''#!/bin/bash
# Generated installer script
# Run with --dry-run to see what would be executed

set -e

DRY_RUN=false

# Parse arguments
for arg in "$@"; do
    if [ "$arg" = "--dry-run" ]; then
        DRY_RUN=true
    fi
done

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

'''

    # Add action-specific functions
    script += '''# Function to execute or dry-run a command
execute() {
    if [ "$DRY_RUN" = true ]; then
        echo "[dry-run] $*"
    else
        eval "$@"
    fi
}

'''

    # Generate actions based on plan
    for module in plan['modules']:
        module_name = module['name']
        for action in module['actions']:
            action_type = action.get('type')

            if action_type == 'install_package':
                manager = action.get('manager', '')
                package = action.get('package', '')
                if manager == 'brew':
                    script += f'execute "brew install {package}"\n'
                elif manager == 'apt':
                    script += f'execute "apt-get install -y {package}"\n'
                elif manager == 'pacman':
                    script += f'execute "pacman -S --noconfirm {package}"\n'
                elif manager == 'dnf':
                    script += f'execute "dnf install -y {package}"\n'
                else:
                    script += f'execute "{manager} install {package}"\n'

            elif action_type == 'install_application':
                app = action.get('application', '')
                # Common app installers
                script += f'''# Install {app}
if command -v brew &> /dev/null; then
    execute "brew install --cask {app}"
elif command -v mas &> /dev/null; then
    execute "mas install $(mas search {app} | head -1 | awk \"{{print $1}}\")"
else
    echo "Warning: No supported package manager found for {app}"
fi

'''

            elif action_type == 'link':
                source = action.get('source', '')
                dest = action.get('dest', '')
                # Make source relative to script directory
                script += f'''# Link {source} to {dest}
execute "mkdir -p $(dirname {dest})"
execute "ln -sf \"$SCRIPT_DIR/{source}\" {dest}"

'''

            elif action_type == 'copy':
                source = action.get('source', '')
                dest = action.get('dest', '')
                script += f'''# Copy {source} to {dest}
execute "mkdir -p $(dirname {dest})"
execute "cp "$SCRIPT_DIR/{source}" {dest}"

'''

            elif action_type == 'run':
                source = action.get('source', '')
                command = action.get('command', '')
                elevated = action.get('elevated', False)
                if elevated:
                    script += f'''# Run elevated command from {source}
execute "sudo {command}"

'''
                else:
                    script += f'''# Run command from {source}
execute "{command}"

'''

            elif action_type == 'set_preference':
                # Preferences are handled in the plan but executed by the installer
                pref_name = action.get('name', '')
                # This would need more complex handling based on preference type
                script += f'# Set preference: {pref_name}\n'
                # Note: Implementation depends on preference structure
                script += ': # TODO: Implement preference setting\n\n'

            elif action_type == 'configure_dock':
                script += '# Configure Dock\n'
                script += ': # TODO: Implement Dock configuration\n\n'

            elif action_type == 'install_runtime':
                manager = action.get('manager', '')
                language = action.get('language', '')
                version = action.get('version', '')
                if manager == 'pyenv':
                    script += f'execute "pyenv install {version}"\n'
                elif manager == 'nvm':
                    script += f'execute "nvm install {version}"\n'
                else:
                    script += f'execute "{manager} install {language} {version}"\n'

            elif action_type == 'install_plugin':
                manager = action.get('manager', '')
                plugin = action.get('plugin', '')
                if manager == 'pyenv':
                    script += f'execute "git clone https://github.com/yyuu/pyenv-virtualenv.git $(pyenv root)/plugins/pyenv-virtualenv"\n'
                elif manager == 'nvm':
                    script += f'execute "nvm install-plugin {plugin}"\n'
                else:
                    script += f'execute "{manager} plugin install {plugin}"\n'

            elif action_type == 'create_virtual_env':
                manager = action.get('manager', '')
                plugin = action.get('plugin', '')
                version = action.get('version', '')
                name = action.get('name', '')
                if manager == 'pyenv' and plugin == 'venv':
                    script += f'execute "pyenv virtualenv {version} {name}"\n'
                else:
                    script += f'execute "{manager} {plugin} create {name} {version}"\n'

    script += '''
echo "Installation complete (or dry-run completed)"
'''

    return script


def create_archive(package_dir: Path) -> Path:
    """Create a tar.gz archive of the package directory.

    Returns path to the archive.
    """
    import tarfile
    archive_path = package_dir.with_suffix('.archive')

    with tarfile.open(archive_path, 'w:gz') as tar:
        tar.add(package_dir, arcname=package_dir.name)

    return archive_path


def leaf_profiles_or_default(manifest_data: Optional[dict], profile_filter: Optional[str]) -> list[str]:
    """Determine which profiles to build.

    If no manifest and no profile filter, use default profile.
    If no manifest but profile filter, error.
    If manifest and no profile filter, use leaf profiles.
    If manifest and profile filter, use that profile.
    """
    if manifest_data is None:
        if profile_filter:
            print(json.dumps({
                "error": "no_manifest",
                "details": "no manifest found but --profile was specified"
            }), file=sys.stderr)
            sys.exit(1)
        return ['default']
    else:
        if profile_filter:
            return [profile_filter]
        else:
            return get_leaf_profiles(manifest_data)


def build_packages(dotfiles_dir: Path, os_name: str, os_version: Optional[str],
                   profile_filter: Optional[str], output_dir: Optional[Path],
                   release: bool) -> dict:
    """Build standalone installer packages.

    Returns dict with packages list for JSON output.
    """
    # Discover modules and manifest
    modules, manifest = discover_modules(dotfiles_dir)
    modules = {k: v for k, v in modules.items() if k != "manifest"}

    if not modules and not manifest:
        # No modules at all
        return {"packages": []}

    # Determine profiles to build
    profiles_to_build = leaf_profiles_or_default(manifest, profile_filter)

    packages = []

    for profile_name in profiles_to_build:
        # Generate plan for this profile
        plan = run_plan(dotfiles_dir, os_name, None, profile_name)

        if not plan['modules']:
            # No modules for this profile, skip
            continue

        # Determine output directory for this package
        if output_dir:
            package_dir = output_dir / f"{profile_name}-package"
        else:
            package_dir = Path(f"{profile_name}-package")

        # Create package directory
        import shutil
        if package_dir.exists():
            shutil.rmtree(package_dir)
        package_dir.mkdir(parents=True)

        # Copy referenced source files
        modules_with_files = copy_source_files(plan, dotfiles_dir, package_dir)

        # Prepare plan for installer - convert absolute paths to relative
        installer_plan = prepare_plan_for_installer(plan, dotfiles_dir)

        # Generate installer script
        installer_script = generate_installer_script(installer_plan, os_name, os_version)
        installer_path = package_dir / "install.sh"
        with open(installer_path, 'w') as f:
            f.write(installer_script)
        installer_path.chmod(0o755)

        # Create archive if --release
        archive_path = None
        if release:
            archive_path = create_archive(package_dir)

        # Record package info
        package_info = {
            "profile": profile_name,
            "os": os_name,
            "directory": str(package_dir),
            "modules": modules_with_files if modules_with_files else [m['name'] for m in plan['modules']]
        }
        if archive_path:
            package_info["archive"] = str(archive_path)

        packages.append(package_info)

    # Sort packages alphabetically by profile name
    packages.sort(key=lambda x: x['profile'])

    return {"packages": packages}


def main():
    parser = argparse.ArgumentParser(
        description='Generate a deterministic plan for developer environments and module dependencies.'
    )
    parser.add_argument(
        'command',
        choices=['plan', 'list-profiles', 'build'],
        help='Command to run ("plan", "list-profiles", or "build")'
    )
    parser.add_argument(
        'dotfiles_dir',
        type=Path,
        help='Path to the dotfiles directory'
    )
    parser.add_argument(
        '--os',
        help='Filter by operating system name (required for build)'
    )
    parser.add_argument(
        '--os-version',
        help='Filter by operating system version'
    )
    parser.add_argument(
        '--module',
        help='Filter by module name'
    )
    parser.add_argument(
        '--profile',
        help='Filter by profile name'
    )
    parser.add_argument(
        '--output',
        type=Path,
        help='Output directory for generated artifacts'
    )
    parser.add_argument(
        '--release',
        action='store_true',
        help='Create archive artifacts in addition to package directories'
    )

    args = parser.parse_args()

    if not args.dotfiles_dir.exists():
        print(f"Error: Directory '{args.dotfiles_dir}' does not exist", file=sys.stderr)
        sys.exit(1)

    if not args.dotfiles_dir.is_dir():
        print(f"Error: '{args.dotfiles_dir}' is not a directory", file=sys.stderr)
        sys.exit(1)

    # Validate build-specific requirements
    if args.command == 'build':
        if not args.os:
            print(json.dumps({
                "error": "missing_flag",
                "details": "--os is required for build"
            }), file=sys.stderr)
            sys.exit(1)

    if args.command == 'plan':
        plan = run_plan(args.dotfiles_dir, args.os, args.module, args.profile)
        print(json.dumps(plan, indent=2))
    elif args.command == 'list-profiles':
        profiles = run_list_profiles(args.dotfiles_dir)
        print(json.dumps(profiles, indent=2))
    elif args.command == 'build':
        result = build_packages(
            args.dotfiles_dir,
            args.os,
            args.os_version,
            args.profile,
            args.output,
            args.release
        )
        print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
