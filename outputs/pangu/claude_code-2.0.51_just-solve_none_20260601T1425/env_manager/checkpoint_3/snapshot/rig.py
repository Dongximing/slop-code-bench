#!/usr/bin/env python3
"""
Rig: A tool for managing developer environments and module dependencies.

This program processes `environments` schema configs, validates dependencies,
and produces a deterministic plan of actions.
"""

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
    # Check schema
    schema = data.get('schema', '')
    if schema != 'environments':
        raise ValidationError(f"{filepath}: schema must be 'environments'")

    # Check version
    version = data.get('version', '')
    if version != '1':
        raise ValidationError(f"{filepath}: unsupported version '{version}'")

    # Check environments is present and non-empty list
    environments = data.get('environments')
    if not isinstance(environments, list) or len(environments) == 0:
        raise ValidationError(f"{filepath}: environments must be a non-empty list")

    for i, env in enumerate(environments):
        # Check language - required, non-empty string
        language = env.get('language', '')
        if not isinstance(language, str) or not language:
            raise ValidationError(f"{filepath}: environments[{i}].language is required and must be a non-empty string")

        # Check versions - required, non-empty list of non-empty strings
        versions = env.get('versions')
        if not isinstance(versions, list) or len(versions) == 0:
            raise ValidationError(f"{filepath}: environments[{i}].versions must be a non-empty list")
        for j, v in enumerate(versions):
            if not isinstance(v, str) or not v:
                raise ValidationError(f"{filepath}: environments[{i}].versions[{j}] must be a non-empty string")

        # Check manager - required object
        manager = env.get('manager')
        if not isinstance(manager, dict):
            raise ValidationError(f"{filepath}: environments[{i}].manager is required and must be an object")

        # Check manager.name - required, non-empty string
        manager_name = manager.get('name', '')
        if not isinstance(manager_name, str) or not manager_name:
            raise ValidationError(f"{filepath}: environments[{i}].manager.name is required and must be a non-empty string")

        # Check plugins if present
        plugins = manager.get('plugins')
        if plugins is not None:
            if not isinstance(plugins, list):
                raise ValidationError(f"{filepath}: environments[{i}].manager.plugins must be a list")

            for j, plugin in enumerate(plugins):
                # Check plugin.name - required, non-empty string
                plugin_name = plugin.get('name', '')
                if not isinstance(plugin_name, str) or not plugin_name:
                    raise ValidationError(f"{filepath}: environments[{i}].manager.plugins[{j}].name is required and must be a non-empty string")

                # Check virtual_environments if present
                venvs = plugin.get('virtual_environments')
                if venvs is not None:
                    if not isinstance(venvs, list):
                        raise ValidationError(f"{filepath}: environments[{i}].manager.plugins[{j}].virtual_environments must be a list")

                    for k, venv in enumerate(venvs):
                        venv_version = venv.get('version', '')
                        venv_name = venv.get('name', '')

                        # Check version - required, non-empty string
                        if not isinstance(venv_version, str) or not venv_version:
                            raise ValidationError(f"{filepath}: environments[{i}].manager.plugins[{j}].virtual_environments[{k}].version is required and must be a non-empty string")

                        # Check version matches parent environment's versions
                        if venv_version not in versions:
                            raise ValidationError(f"{filepath}: environments[{i}].manager.plugins[{j}].virtual_environments[{k}].version: '{venv_version}' is not in the environment's versions list")

                        # Check name - required, non-empty string
                        if not isinstance(venv_name, str) or not venv_name:
                            raise ValidationError(f"{filepath}: environments[{i}].manager.plugins[{j}].virtual_environments[{k}].name is required and must be a non-empty string")

    return data


def validate_module_config(data: dict, filepath: Path) -> dict:
    """Validate module schema according to specification."""
    schema = data.get('schema', '')
    if schema != 'module':
        raise ValidationError(f"{filepath}: schema must be 'module'")

    # Check version
    version = data.get('version', '')
    if version != '1':
        raise ValidationError(f"{filepath}: unsupported version '{version}'")

    # Validate depends_on if present - must be a list of non-empty strings
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


def parse_config_file(filepath: Path) -> tuple[str, Optional[dict], Optional[dict], Optional[dict], Optional[dict]]:
    """Parse a config file and return (module_name, module_data, environments_data, preferences_data, dock_data)."""
    data = load_yaml_file(filepath)
    schema = data.get('schema', '')

    if schema == 'environments':
        validate_environments_config(data, filepath)
        # The module name is the parent directory
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
    else:
        raise ValidationError(f"{filepath}: invalid schema '{schema}'")


def discover_modules(dotfiles_dir: Path) -> dict[str, dict]:
    """Discover all modules in the dotfiles directory."""
    modules = {}

    for config_file in dotfiles_dir.rglob('*.yaml'):
        try:
            module_name, module_data, environments_data, preferences_data, dock_data = parse_config_file(config_file)

            if module_name not in modules:
                modules[module_name] = {
                    'name': module_name,
                    'module_data': None,
                    'environments_data': None,
                    'preferences_data': None,
                    'dock_data': None,
                }

            # Merge module data for module schema
            if module_data is not None:
                existing = modules[module_name]['module_data']
                if existing is None:
                    modules[module_name]['module_data'] = module_data
                else:
                    # Merge depends_on lists as a union, removing duplicates
                    existing_depends = set(existing.get('depends_on', []))
                    new_depends = set(module_data.get('depends_on', []))
                    merged = existing_depends | new_depends
                    existing['depends_on'] = list(merged)
                    # Also merge other fields if needed (actions, os, etc.)
                    # For simplicity, we keep the first one for other fields

            if environments_data is not None:
                modules[module_name]['environments_data'] = environments_data

            if preferences_data is not None:
                modules[module_name]['preferences_data'] = preferences_data

            if dock_data is not None:
                modules[module_name]['dock_data'] = dock_data

        except ValidationError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    return modules


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
            # Found a cycle - extract it
            start_idx = cycle_stack.index(neighbor)
            cycle = cycle_stack[start_idx:] + [neighbor]
            return cycle

    cycle_stack.pop()
    recursion_stack.remove(node)
    return None


def detect_cycle(graph: dict[str, list[str]]) -> Optional[list[str]]:
    """Detect a cycle in the dependency graph using DFS."""
    visited = set()
    recursion_stack = set()
    cycle_stack = []

    for node in graph:
        if node not in visited:
            result = detect_cycle_dfs(node, graph, visited, recursion_stack, cycle_stack)
            if result:
                return result

    return None


def topological_sort(graph: dict[str, list[str]], modules: dict[str, dict]) -> list[str]:
    """Perform topological sort on the dependency graph."""
    # Check for cycles first
    cycle = detect_cycle(graph)
    if cycle:
        print(json.dumps({"error": "circular_dependency", "cycle": cycle}), file=sys.stderr)
        sys.exit(1)

    # Kahn's algorithm for topological sort with alphabetical tie-breaking
    in_degree = {node: 0 for node in graph}
    for node, deps in graph.items():
        for dep in deps:
            if dep in in_degree:
                in_degree[dep] = in_degree.get(dep, 0) + 1
            else:
                in_degree[dep] = 1

    # Initialize queue with nodes having no incoming edges
    queue = [node for node in in_degree if in_degree[node] == 0]
    queue.sort()  # Alphabetical tie-breaker

    result = []
    while queue:
        node = queue.pop(0)
        result.append(node)

        for neighbor in graph.get(node, []):
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

        # Keep queue sorted for alphabetical tie-breaking
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
    """Generate environment actions from environments config following the strict ordering."""
    actions = []
    environments = environments_data.get('environments', [])

    for env in environments:
        language = env['language']
        versions = env['versions']
        manager = env['manager']['name']
        plugins = env['manager'].get('plugins', [])

        # Step 1: All install_runtime actions for this environment's versions, in order
        for version in versions:
            actions.append({
                "type": "install_runtime",
                "language": language,
                "version": version,
                "manager": manager
            })

        # Step 2: All install_plugin actions, in plugin order
        for plugin in plugins:
            plugin_name = plugin['name']
            actions.append({
                "type": "install_plugin",
                "manager": manager,
                "plugin": plugin_name
            })

        # Step 3: All create_virtual_env actions, grouped by plugin order
        # and preserving each plugin's own virtual environment order
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
        # Filter by module name if specified
        if module_filter and name != module_filter:
            continue

        # Filter by OS if specified
        module_data = module['module_data']
        if module_data and 'os' in module_data:
            os_config = module_data['os']
            if os_filter and os_config.get('name') != os_filter:
                continue

        filtered[name] = module

    return filtered


def plan_module_actions(module: dict[str, Any], dotfiles_dir: Path) -> list[dict]:
    """Generate actions for a single module following the strict ordering:"

    Order within one module:
    1. install_package, sorted alphabetically by package
    2. install_application, sorted alphabetically by application
    3. file actions (link, copy, run) in config order
    4. set_preference in config order
    5. configure_dock
    6. environment actions, processed per environment in config order
    """
    actions = []
    module_name = module['name']
    module_data = module['module_data']
    environments_data = module['environments_data']

    # Step 1: install_package actions, sorted alphabetically by package
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

    # Step 2: install_application actions, sorted alphabetically by application
    if module_data:
        applications = module_data.get('applications', [])
        for app in sorted(applications):
            actions.append({
                "type": "install_application",
                "application": app
            })

    # Step 3: file actions (link, copy, run) in config order
    if module_data:
        file_actions = module_data.get('actions', [])
        for action in file_actions:
            action_type = action.get('type')
            if action_type in ('link', 'copy', 'run'):
                new_action = dict(action)
                # Resolve source path relative to dotfiles
                if 'source' in action:
                    source = action['source']
                    if not source.startswith('/'):
                        # Relative to dotfiles
                        new_action['source'] = str(dotfiles_dir / module_name / source)
                    # elevated field should be preserved
                    if 'elevated' in action:
                        new_action['elevated'] = action['elevated']
                actions.append(new_action)

    # Step 4: set_preference in config order
    if module_data:
        preferences = module_data.get('preferences', [])
        for pref in preferences:
            actions.append({
                "type": "set_preference",
                **pref
            })

    # Step 5: configure_dock
    if module_data and 'dock' in module_data:
        actions.append({"type": "configure_dock"})

    # Step 6: environment actions, processed per environment in config order
    if environments_data:
        env_actions = generate_environment_actions(module_name, environments_data)
        actions.extend(env_actions)

    return actions


def run_plan(dotfiles_dir: Path, os_filter: Optional[str] = None, module_filter: Optional[str] = None) -> dict:
    """Generate the plan for the given dotfiles directory."""
    # 1. Discover modules
    modules = discover_modules(dotfiles_dir)

    if not modules:
        return {"modules": []}

    # 2. Filter modules
    filtered_modules = filter_modules(modules, os_filter, module_filter)

    if not filtered_modules:
        return {"modules": []}

    # 3. Build dependency graph
    graph = build_dependency_graph(filtered_modules)

    # 4. Validate dependencies exist
    validate_dependencies_exist(graph, filtered_modules)

    # 5. Topological sort to get module order
    ordered_names = topological_sort(graph, filtered_modules)

    # 6. Generate plan with actions for each module
    plan_modules = []
    for name in ordered_names:
        module = filtered_modules[name]
        actions = plan_module_actions(module, dotfiles_dir)
        plan_modules.append({
            "name": name,
            "actions": actions
        })

    return {"modules": plan_modules}


def main():
    parser = argparse.ArgumentParser(
        description='Generate a deterministic plan for developer environments and module dependencies.'
    )
    parser.add_argument(
        'command',
        choices=['plan'],
        help='Command to run (currently only "plan")'
    )
    parser.add_argument(
        'dotfiles_dir',
        type=Path,
        help='Path to the dotfiles directory'
    )
    parser.add_argument(
        '--os',
        help='Filter by operating system name'
    )
    parser.add_argument(
        '--module',
        help='Filter by module name'
    )

    args = parser.parse_args()

    # Validate dotfiles_dir exists
    if not args.dotfiles_dir.exists():
        print(f"Error: Directory '{args.dotfiles_dir}' does not exist", file=sys.stderr)
        sys.exit(1)

    if not args.dotfiles_dir.is_dir():
        print(f"Error: '{args.dotfiles_dir}' is not a directory", file=sys.stderr)
        sys.exit(1)

    if args.command == 'plan':
        plan = run_plan(args.dotfiles_dir, args.os, args.module)
        print(json.dumps(plan, indent=2))


if __name__ == '__main__':
    main()
