#!/usr/bin/env python3
"""
Rig - System Preferences, Dock Layout, and Conflict Detection

Generates a JSON execution plan from directory tree configuration files.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

# OS versions in order from oldest to newest
OS_VERSIONS = [
    "yosemite",
    "el_capitan",
    "sierra",
    "high_sierra",
    "mojave",
    "catalina",
    "big_sur",
    "monterey",
    "ventura",
    "sonoma",
]


class ProfileError(Exception):
    """Raised when profile-related errors occur."""

    def __init__(self, message: str, error_type: str, details: dict = None):
        self.message = message
        self.error_type = error_type
        self.details = details or {}
        super().__init__(message)


class ValidationError(Exception):
    """Raised when configuration validation fails."""

    def __init__(self, message: str, error_type: str = "validation", details: dict = None):
        self.message = message
        self.error_type = error_type
        self.details = details or {}
        super().__init__(message)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Generate JSON execution plan from directory configuration"
    )
    parser.add_argument("command", choices=["plan", "list-profiles"], help="Command to run")
    parser.add_argument("dir", type=Path, help="Directory containing configuration files")
    parser.add_argument("--os", help="Filter by operating system")
    parser.add_argument("--os-version", help="Filter by OS version")
    parser.add_argument("--module", action="append", dest="modules", help="Filter by module name(s)")
    parser.add_argument("--profile", help="Filter by profile name")
    return parser.parse_args()


def load_yaml_file(path: Path) -> dict:
    """Load and parse a YAML file."""
    with open(path, "r") as f:
        return yaml.safe_load(f)


def load_manifest(base_dir: Path) -> dict:
    """Load and validate a manifest file from the base directory.

    Returns the manifest data if found and valid, or None if no manifest file exists.
    """
    # Look for manifest files in the root directory
    manifest_files = list(base_dir.glob("manifest.yaml")) + list(base_dir.glob("manifest.yml"))

    if not manifest_files:
        return None

    if len(manifest_files) > 1:
        raise ValidationError("multiple 'manifest' schemas found — only one is allowed")

    manifest_path = manifest_files[0]
    data = load_yaml_file(manifest_path)

    if data is None:
        return None

    # Validate manifest structure
    if "version" not in data or data.get("version") != "1":
        return None

    if data.get("schema") != "manifest":
        return None

    return data


def validate_manifest(manifest: dict, base_dir: Path) -> dict:
    """Validate a manifest and return validated profile definitions.

    Returns:
        dict with 'profiles' key mapping profile names to their resolved module lists
    """
    if manifest is None:
        return None

    # Check profiles is present and non-empty
    if "profiles" not in manifest or not isinstance(manifest["profiles"], dict) or not manifest["profiles"]:
        raise ProfileError("profiles must be a non-empty object", error_type="validation")

    profiles = manifest["profiles"]
    profile_names = set(profiles.keys())

    # Validate each profile
    for name, profile in profiles.items():
        if "modules" not in profile or not isinstance(profile["modules"], list) or len(profile["modules"]) == 0:
            raise ProfileError(
                f"profiles.{name}.modules must be a non-empty list",
                error_type="validation"
            )

        # Validate each module entry is a non-empty string
        for i, module in enumerate(profile["modules"]):
            if not isinstance(module, str) or not module:
                raise ProfileError(
                    f"profiles.{name}.modules[{i}] must be a non-empty string",
                    error_type="validation"
                )

        # Validate extends reference
        if "extends" in profile:
            extends_to = profile["extends"]
            if extends_to not in profile_names:
                raise ProfileError(
                    f"profiles.{name}.extends: unknown profile '{extends_to}'",
                    error_type="validation"
                )

    return manifest


def resolve_profile_inheritance(manifest: dict) -> dict:
    """Resolve profile inheritance and return resolved module sets.

    Returns:
        dict mapping profile names to their resolved module lists (deduplicated)
    """
    profiles = manifest["profiles"]
    resolved = {}

    def resolve_profile(name: str, visited: set) -> list:
        """Recursively resolve a profile's modules, detecting cycles."""
        if name in visited:
            # Cycle detected
            cycle = list(visited) + [name]
            raise ProfileError(
                f"circular inheritance detected: {' -> '.join(cycle)}",
                error_type="circular_inheritance",
                details={"cycle": cycle}
            )

        if name in resolved:
            return resolved[name]

        profile = profiles[name]
        modules = []

        if "extends" in profile:
            parent_name = profile["extends"]
            visited.add(name)
            parent_modules = resolve_profile(parent_name, visited)
            modules = parent_modules[:]
            visited.remove(name)

        # Add own modules, preserving order
        for mod in profile["modules"]:
            if mod not in modules:
                modules.append(mod)

        resolved[name] = modules
        return modules

    # Resolve all profiles
    for name in profiles:
        if name not in resolved:
            resolve_profile(name, set())

    return resolved


def validate_modules_exist(resolved_profiles: dict, available_modules: set) -> None:
    """Validate that all resolved modules exist in the discovered modules.

    Raises ProfileError for invalid profiles.
    """
    invalid_profiles = []

    for profile_name, modules in resolved_profiles.items():
        missing = [m for m in modules if m not in available_modules]
        if missing:
            missing.sort()
            invalid_profiles.append({
                "name": profile_name,
                "missing_modules": missing
            })

    if invalid_profiles:
        # Report alphabetically first invalid profile
        invalid_profiles.sort(key=lambda x: x["name"])
        first = invalid_profiles[0]
        raise ProfileError(
            f"Profile '{first['name']}' references missing modules: {', '.join(first['missing_modules'])}",
            error_type="invalid_profile",
            details={
                "profile": first["name"],
                "missing_modules": first["missing_modules"]
            }
        )


def validate_preference_entry(entry: dict, index: int) -> dict:
    """Validate a preferences entry and return validated data."""
    # Check required fields
    required = ["name", "domain", "key", "value", "value_type", "apply_command"]
    for field in required:
        if field not in entry:
            raise ValidationError(f"preferences[{index}].{field} is required")

    # Validate value_type
    valid_value_types = {"bool", "int", "string", "float"}
    value_type = entry["value_type"]
    if value_type not in valid_value_types:
        raise ValidationError(
            f"preferences[{index}].value_type: must be 'bool', 'int', 'string', or 'float', got '{value_type}'"
        )

    # Validate min_version
    if "min_version" in entry:
        min_version = entry["min_version"]
        if min_version not in OS_VERSIONS:
            raise ValidationError(
                f"preferences[{index}].min_version: unknown version '{min_version}'"
            )

    # Validate max_version
    if "max_version" in entry:
        max_version = entry["max_version"]
        if max_version not in OS_VERSIONS:
            raise ValidationError(
                f"preferences[{index}].max_version: unknown version '{max_version}'"
            )

    # Validate min_version <= max_version
    if "min_version" in entry and "max_version" in entry:
        min_ver = entry["min_version"]
        max_ver = entry["max_version"]
        if OS_VERSIONS.index(min_ver) > OS_VERSIONS.index(max_ver):
            raise ValidationError(
                f"preferences[{index}]: min_version '{min_ver}' is later than max_version '{max_ver}'"
            )

    # Default enabled to True if not specified
    if "enabled" not in entry:
        entry = {**entry, "enabled": True}

    return entry


def validate_dock_config(config: dict, file_path: Path, dock_configs: list) -> dict:
    """Validate a dock config and return validated data."""
    # Check os field
    if "os" not in config or config["os"] != "macos":
        raise ValidationError("dock: 'os' is required and must be 'macos'")

    # Check items field
    if "items" not in config or not isinstance(config["items"], list) or len(config["items"]) == 0:
        raise ValidationError("dock.items must be a non-empty list of strings")

    # Validate items are strings
    for item in config["items"]:
        if not isinstance(item, str):
            raise ValidationError("dock.items must be a non-empty list of strings")

    # Track dock configs globally
    dock_configs.append(file_path)

    return config


def validate_depends_on(depends_on: any, module_name: str) -> list:
    """Validate depends_on field and return list of dependencies."""
    if depends_on is None:
        return []

    if not isinstance(depends_on, list):
        raise ValidationError(f"{module_name}: depends_on must be a list")

    deps = []
    for i, dep in enumerate(depends_on):
        if not isinstance(dep, str) or not dep:
            raise ValidationError(f"{module_name}: depends_on[{i}] must be a non-empty string")
        deps.append(dep)

    # Deduplicate
    return list(dict.fromkeys(deps))


def validate_environments_config(config: dict, file_path: Path) -> dict:
    """Validate an environments config and return validated data."""
    if "environments" not in config or not isinstance(config["environments"], list) or len(config["environments"]) == 0:
        raise ValidationError("environments: must be a non-empty list")

    validated_envs = []

    for i, env in enumerate(config["environments"]):
        # Validate language
        if "language" not in env or not isinstance(env["language"], str) or not env["language"]:
            raise ValidationError(f"environments[{i}].language is required and must be a non-empty string")

        # Validate versions
        if "versions" not in env or not isinstance(env["versions"], list) or len(env["versions"]) == 0:
            raise ValidationError(f"environments[{i}].versions is required and must be a non-empty list of non-empty strings")

        # Check each version is a non-empty string
        for j, ver in enumerate(env["versions"]):
            if not isinstance(ver, str) or not ver:
                raise ValidationError(f"environments[{i}].versions[{j}] must be a non-empty string")

        # Validate manager
        if "manager" not in env or not isinstance(env["manager"], dict):
            raise ValidationError(f"environments[{i}].manager is required and must be an object")

        manager = env["manager"]

        if "name" not in manager or not isinstance(manager["name"], str) or not manager["name"]:
            raise ValidationError(f"environments[{i}].manager.name is required and must be a non-empty string")

        # Validate plugins if present
        plugins = manager.get("plugins", [])
        validated_plugins = []

        for j, plugin in enumerate(plugins):
            if "name" not in plugin or not isinstance(plugin["name"], str) or not plugin["name"]:
                raise ValidationError(f"environments[{i}].manager.plugins[{j}].name is required and must be a non-empty string")

            validated_plugin = {"name": plugin["name"]}

            # Validate virtual_environments if present
            venvs = plugin.get("virtual_environments", [])
            validated_venvs = []

            for k, venv in enumerate(venvs):
                if "version" not in venv or not isinstance(venv["version"], str) or not venv["version"]:
                    raise ValidationError(f"environments[{i}].manager.plugins[{j}].virtual_environments[{k}].version is required and must be a non-empty string")

                if "name" not in venv or not isinstance(venv["name"], str) or not venv["name"]:
                    raise ValidationError(f"environments[{i}].manager.plugins[{j}].virtual_environments[{k}].name is required and must be a non-empty string")

                # Check version is in parent environment's versions
                if venv["version"] not in env["versions"]:
                    raise ValidationError(
                        f"environments[{i}].manager.plugins[{j}].virtual_environments[{k}].version: "
                        f"'{venv['version']}' is not in the environment's versions list",
                        details={
                            "path": f"environments[{i}].manager.plugins[{j}].virtual_environments[{k}].version",
                            "version": venv["version"],
                            "allowed_versions": env["versions"]
                        }
                    )

                validated_venvs.append({
                    "version": venv["version"],
                    "name": venv["name"]
                })

            if validated_venvs:
                validated_plugin["virtual_environments"] = validated_venvs

            validated_plugins.append(validated_plugin)

        if validated_plugins:
            manager["plugins"] = validated_plugins

        validated_envs.append({
            "language": env["language"],
            "versions": env["versions"],
            "manager": manager
        })

    config["_validated_environments"] = validated_envs
    return config


def discover_configs(base_dir: Path, os_filter: str = None, module_filter: list = None) -> dict:
    """Discover and load all configuration files from directory tree."""
    configs = {
        "modules": {},  # module_name -> {configs, file_paths}
        "dock_configs": [],  # list of file paths with dock schema
        "manifest": None,  # manifest data if found
    }

    # First, check for manifest in root directory
    manifest = load_manifest(base_dir)
    if manifest:
        # Validate manifest
        try:
            validate_manifest(manifest, base_dir)
        except ProfileError as e:
            raise ValidationError(e.message)
        configs["manifest"] = manifest

        # Check if manifest is inside a module directory
        # Look for manifest.yaml or manifest.yml in subdirectories (module directories)
        for yaml_file in base_dir.rglob("manifest.yaml"):
            module_dir = yaml_file.parent
            module_name = module_dir.name
            if module_name == base_dir.name:
                continue  # skip root
            # Found manifest in module directory - this is invalid
            raise ValidationError(f"manifest schema must be in the root directory, found in '{module_name}'")
        for yaml_file in base_dir.rglob("manifest.yml"):
            module_dir = yaml_file.parent
            module_name = module_dir.name
            if module_name == base_dir.name:
                continue  # skip root
            # Found manifest in module directory - this is invalid
            raise ValidationError(f"manifest schema must be in the root directory, found in '{module_name}'")

    # Find all YAML files (excluding manifest.yaml/manifest.yml in root)
    yaml_files = list(base_dir.glob("*.yaml")) + list(base_dir.glob("*.yml"))
    # Filter out manifest files from root
    yaml_files = [f for f in yaml_files if f.name not in ("manifest.yaml", "manifest.yml")]
    # Also find YAML files in subdirectories
    yaml_files += list(base_dir.rglob("*.yaml"))
    yaml_files += list(base_dir.rglob("*.yml"))
    # Filter out manifest files from subdirectories
    yaml_files = [f for f in yaml_files if f.name not in ("manifest.yaml", "manifest.yml")]

    for yaml_file in yaml_files:
        try:
            data = load_yaml_file(yaml_file)
        except yaml.YAMLError as e:
            raise ValidationError(f"Invalid YAML in {yaml_file}: {e}")

        # Validate version and schema
        if "version" not in data or data["version"] != "1":
            continue  # Skip files without version or wrong version

        if "schema" not in data:
            continue  # Skip files without schema

        schema = data["schema"]

        # Determine module name from directory
        module_dir = yaml_file.parent
        module_name = module_dir.name

        # Apply module filter if specified
        if module_filter and module_name not in module_filter:
            continue

        # Initialize module if needed
        if module_name not in configs["modules"]:
            configs["modules"][module_name] = {
                "configs": [],
                "file_paths": [],
                "dir": module_dir,
                "file_actions": [],  # Track file actions for conflict detection
                "depends_on": set(),  # Track merged depends_on for this module
            }

        module_info = configs["modules"][module_name]
        module_info["configs"].append(data)
        module_info["file_paths"].append(yaml_file)

        # Collect depends_on from this config file
        if "depends_on" in data:
            deps = validate_depends_on(data["depends_on"], module_name)
            module_info["depends_on"].update(deps)

        # Validate based on schema
        if schema == "preferences":
            # Validate preferences entries
            preferences = data.get("preferences", [])
            validated_prefs = []
            for i, pref in enumerate(preferences):
                validated_prefs.append(validate_preference_entry(pref, i))
            data["_validated_preferences"] = validated_prefs

        elif schema == "dock":
            validate_dock_config(data, yaml_file, configs["dock_configs"])

        elif schema == "module":
            # Extract file actions from module config
            if "files" in data:
                for file_cfg in data["files"]:
                    module_info["file_actions"].append({
                        "type": file_cfg.get("type"),
                        "source": file_cfg.get("source"),
                        "destination": file_cfg.get("destination"),
                        "config_file": yaml_file,
                    })

        elif schema == "environments":
            validate_environments_config(data, yaml_file)

        else:
            raise ValidationError(f"Unrecognized schema '{schema}'")

    # Check for multiple dock configs
    if len(configs["dock_configs"]) > 1:
        dock_paths = [str(f) for f in configs["dock_configs"]]
        raise ValidationError(
            "multiple 'dock' schemas found — only one is allowed",
            details={"files": dock_paths}
        )

    return configs


def is_os_version_in_range(version: str, min_ver: str = None, max_ver: str = None) -> bool:
    """Check if a version is within the specified range."""
    if version not in OS_VERSIONS:
        return False

    ver_idx = OS_VERSIONS.index(version)

    if min_ver:
        min_idx = OS_VERSIONS.index(min_ver)
        if ver_idx < min_idx:
            return False

    if max_ver:
        max_idx = OS_VERSIONS.index(max_ver)
        if ver_idx > max_idx:
            return False

    return True


def is_unknown_os_version(version: str) -> bool:
    """Check if a version is not in our known versions list."""
    return version not in OS_VERSIONS


def filter_preferences_by_os_version(preferences: list, os_version: str) -> list:
    """Filter preferences based on OS version."""
    filtered = []
    for pref in preferences:
        # Skip disabled preferences
        if not pref.get("enabled", True):
            continue

        min_ver = pref.get("min_version")
        max_ver = pref.get("max_version")

        # If no version constraints, include
        if not min_ver and not max_ver:
            filtered.append(pref)
            continue

        # Check if version is in range
        if is_os_version_in_range(os_version, min_ver, max_ver):
            filtered.append(pref)

    return filtered


def find_cycle(modules: dict) -> list:
    """Find a cycle in the dependency graph using DFS.

    Returns a cycle as a list [a, b, c, a] if one exists, or empty list if none.
    """
    visited = set()  # Fully processed nodes
    path = set()     # Nodes in current DFS path
    path_stack = []  # Track path order for cycle reconstruction

    def dfs(node):
        visited.add(node)
        path.add(node)
        path_stack.append(node)

        for neighbor in modules.get(node, {}).get("depends_on", []):
            if neighbor not in modules:
                continue  # Missing dependencies handled separately
            if neighbor not in visited:
                cycle = dfs(neighbor)
                if cycle:
                    return cycle
            elif neighbor in path:
                # Found a cycle - reconstruct it
                start_idx = path_stack.index(neighbor)
                cycle = path_stack[start_idx:] + [neighbor]
                return cycle

        path_stack.pop()
        path.remove(node)
        return []

    for module_name in sorted(modules.keys()):
        if module_name not in visited:
            cycle = dfs(module_name)
            if cycle:
                return cycle

    return []


def check_missing_dependencies(configs: dict) -> dict:
    """Check for missing dependencies.

    Returns dict with 'module' and 'missing' keys if missing dependency found, None otherwise.
    """
    module_names = set(configs["modules"].keys())

    for module_name, module_info in configs["modules"].items():
        for dep in module_info.get("depends_on", set()):
            if dep not in module_names:
                return {
                    "module": module_name,
                    "missing": dep
                }

    return None


def get_profile_modules(configs: dict, profile_name: str) -> list:
    """Get resolved modules for a profile, including transitive dependencies.

    Returns:
        List of module names that belong to the profile, sorted alphabetically.
    """
    manifest = configs.get("manifest")
    if not manifest:
        raise ProfileError(
            "no manifest found but --profile was specified",
            error_type="no_manifest",
            details={"message": "no manifest found but --profile was specified"}
        )

    # Resolve profiles
    resolved_profiles = resolve_profile_inheritance(manifest)
    validate_manifest(manifest, None)  # Re-validate to check for cycles

    if profile_name not in resolved_profiles:
        raise ProfileError(
            f"unknown profile '{profile_name}'",
            error_type="unknown_profile",
            details={
                "name": profile_name,
                "details": f"unknown profile '{profile_name}'"
            }
        )

    profile_modules = resolved_profiles[profile_name]

    # Validate modules exist
    available_modules = set(configs["modules"].keys())
    validate_modules_exist({profile_name: profile_modules}, available_modules)

    # Include transitive dependencies
    final_modules = set()
    to_process = list(profile_modules)

    while to_process:
        module = to_process.pop()
        if module in final_modules:
            continue
        final_modules.add(module)

        # Add dependencies
        if module in configs["modules"]:
            deps = configs["modules"][module].get("depends_on", set())
            for dep in deps:
                if dep not in final_modules:
                    to_process.append(dep)

    return sorted(final_modules)


def filter_modules_by_profile(configs: dict, profile_name: str, module_filter: list = None) -> dict:
    """Filter modules based on profile and optional module filter.

    Returns:
        Filtered configs dict with only the profile-relevant modules.
    """
    profile_modules = get_profile_modules(configs, profile_name)

    # Apply module filter if specified (intersection)
    if module_filter:
        profile_modules = [m for m in profile_modules if m in module_filter]

    # Filter the modules in configs
    filtered_modules = {}
    for module_name in profile_modules:
        if module_name in configs["modules"]:
            filtered_modules[module_name] = configs["modules"][module_name]

    result = configs.copy()
    result["modules"] = filtered_modules

    return result


def topological_sort_modules(configs: dict, profile_order: list = None) -> list:
    """Sort modules topologically by dependencies, with profile-order tie-breaking.

    Returns ordered list of module names.

    Args:
        configs: Configuration dict containing modules
        profile_order: Optional list of module names from the profile (preserves order when ties occur)
    """
    modules = configs["modules"]
    module_names = list(modules.keys())

    # Kahn's algorithm for topological sort
    in_degree = {name: 0 for name in module_names}
    graph = {name: set() for name in module_names}

    for module_name in module_names:
        for dep in modules[module_name].get("depends_on", []):
            if dep in module_names:  # Only count actual modules
                graph[dep].add(module_name)
                in_degree[module_name] += 1

    # Start with nodes that have no incoming edges
    # Sort by profile order if provided, else alphabetically
    if profile_order:
        def profile_key(name):
            try:
                return profile_order.index(name)
            except ValueError:
                return len(profile_order)  # Put non-profile modules at end
        queue = sorted([name for name in module_names if in_degree[name] == 0], key=profile_key)
    else:
        queue = sorted([name for name in module_names if in_degree[name] == 0])

    result = []

    while queue:
        # Take the first module
        module = queue.pop(0)
        result.append(module)

        # Get all neighbors
        neighbors = list(graph[module])

        # Sort neighbors by profile order if provided, else alphabetically
        if profile_order:
            neighbors.sort(key=profile_key)
        else:
            neighbors.sort()

        for neighbor in neighbors:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)
                # Keep queue sorted
                if profile_order:
                    queue.sort(key=profile_key)
                else:
                    queue.sort()

    return result


def check_conflicts(configs: dict, os_filter: str = None, os_version: str = None, base_dir: Path = None) -> list:
    """Check for destination conflicts in link and copy actions."""
    # Track destinations and their sources
    dest_map = {}  # destination -> list of (module, type, source)

    for module_name, module_info in configs["modules"].items():
        # Check if this module should be included based on OS filter
        include_module = False
        for config in module_info["configs"]:
            if config.get("schema") == "module":
                os_config = config.get("os", {})
                os_name = os_config.get("name")
                if not os_filter or os_name == os_filter:
                    include_module = True
                    break
        else:
            # No module schema found or no OS filter match, but might have other configs
            # Check if module has non-os-specific configs
            for config in module_info["configs"]:
                if config.get("schema") in ("preferences", "dock"):
                    include_module = True
                    break

        if not include_module:
            continue

        # Process file actions for this module
        for file_act in module_info.get("file_actions", []):
            if file_act["type"] not in ("link", "copy"):
                continue

            destination = file_act.get("destination")
            if not destination:
                continue

            if destination not in dest_map:
                dest_map[destination] = []

            dest_map[destination].append({
                "module": module_name,
                "type": file_act["type"],
                "source": file_act.get("source", ""),
            })

    # Build conflict list
    conflicts = []
    for destination in sorted(dest_map.keys()):
        sources = dest_map[destination]
        if len(sources) > 1:
            # Sort sources by module, then source, then type
            sources.sort(key=lambda x: (x["module"], x["source"], x["type"]))
            conflicts.append({
                "destination": destination,
                "sources": sources,
            })

    return conflicts


def generate_environment_actions(env_config: dict) -> list:
    """Generate environment actions from an environments config.

    Action order within one environment:
    1. All install_runtime actions for that environment's versions, in order the versions appear
    2. All install_plugin actions, in plugin order
    3. All create_virtual_env actions, grouped by plugin order preserving each plugin's venv order
    """
    actions = []

    for env in env_config["_validated_environments"]:
        language = env["language"]
        versions = env["versions"]
        manager_name = env["manager"]["name"]
        plugins = env["manager"].get("plugins", [])

        # 1. install_runtime actions - one per version, in order
        for version in versions:
            actions.append({
                "type": "install_runtime",
                "language": language,
                "version": version,
                "manager": manager_name,
            })

        # 2. install_plugin actions - one per plugin, in plugin order
        for plugin in plugins:
            actions.append({
                "type": "install_plugin",
                "manager": manager_name,
                "plugin": plugin["name"],
            })

        # 3. create_virtual_env actions - grouped by plugin order, preserving venv order within each plugin
        for plugin in plugins:
            for venv in plugin.get("virtual_environments", []):
                actions.append({
                    "type": "create_virtual_env",
                    "language": language,
                    "manager": manager_name,
                    "plugin": plugin["name"],
                    "version": venv["version"],
                    "name": venv["name"],
                })

    return actions


def filter_module_by_os(module_info: dict, os_filter: str = None) -> bool:
    """Check if a module should be included based on OS filter.

    Returns True if module should be included, False if filtered out.
    """
    if not os_filter:
        return True

    # Check if any module config matches the OS filter
    for config in module_info["configs"]:
        if config.get("schema") == "module":
            os_config = config.get("os", {})
            os_name = os_config.get("name")
            if os_name == os_filter:
                return True

    # If no module schema matches the OS filter, skip this module entirely.
    # Non-module configs (preferences, dock, environments) don't make a module
    # included if the module schema's os field doesn't match.
    return False


def generate_plan(configs: dict, os_filter: str = None, os_version: str = None, profile_name: str = None, module_filter: list = None) -> dict:
    """Generate the execution plan from validated configs.

    Args:
        configs: Configuration dict
        os_filter: OS to filter by
        os_version: OS version to filter by
        profile_name: Profile name to filter by (optional)
        module_filter: Specific modules to filter to (optional)
    """
    # First, check for dependency errors
    module_names = set(configs["modules"].keys())

    # Check for circular dependency
    cycle = find_cycle(configs["modules"])
    if cycle:
        raise ValueError(json.dumps({
            "error": "circular_dependency",
            "cycle": cycle
        }))

    # Check for missing dependencies
    missing = check_missing_dependencies(configs)
    if missing:
        raise ValueError(json.dumps({
            "error": "missing_dependency",
            "module": missing["module"],
            "missing": missing["missing"]
        }))

    # Apply profile filtering if specified
    filtered_configs = configs
    if profile_name:
        filtered_configs = filter_modules_by_profile(configs, profile_name, module_filter)
        # Re-validate cycles after profile filtering
        cycle = find_cycle(filtered_configs["modules"])
        if cycle:
            raise ProfileError(
                f"circular inheritance detected: {' -> '.join(cycle)}",
                error_type="circular_inheritance",
                details={"cycle": cycle}
            )

    # Filter modules by OS
    filtered_modules = {}
    for module_name, module_info in filtered_configs["modules"].items():
        if filter_module_by_os(module_info, os_filter):
            filtered_modules[module_name] = module_info

    # Rebuild configs with filtered modules for dependency checking
    filtered_configs["modules"] = filtered_modules

    # Re-check dependencies after filtering
    module_names = set(filtered_configs["modules"].keys())

    # Check for missing dependencies after filtering
    missing = check_missing_dependencies(filtered_configs)
    if missing:
        raise ValueError(json.dumps({
            "error": "missing_dependency",
            "module": missing["module"],
            "missing": missing["missing"]
        }))

    # Check for circular dependency again after filtering
    cycle = find_cycle(filtered_configs["modules"])
    if cycle:
        raise ValueError(json.dumps({
            "error": "circular_dependency",
            "cycle": cycle
        }))

    # For profile ordering, get resolved profile modules
    resolved_modules_order = None
    if profile_name:
        resolved_modules_order = get_profile_modules(filtered_configs, profile_name)

    # Sort modules topologically with profile order for tie-breaking
    ordered_module_names = topological_sort_modules(filtered_configs, resolved_modules_order)

    modules_plan = []

    for module_name in ordered_module_names:
        module_info = configs["modules"][module_name]
        actions = []

        # Collect actions from different schemas
        package_actions = []
        application_actions = []
        file_actions = []
        preference_actions = []
        dock_action = None
        env_actions = []

        for config in module_info["configs"]:
            schema = config.get("schema")

            if schema == "module":
                # Handle module config
                os_config = config.get("os", {})
                os_name = os_config.get("name")

                # Check OS filter
                if os_filter and os_name != os_filter:
                    continue

                # Handle packages
                package_manager = os_config.get("package_manager")
                packages = os_config.get("packages", [])
                for pkg in packages:
                    package_actions.append({
                        "type": "install_package",
                        "manager": package_manager,
                        "package": pkg,
                    })

                # Handle applications
                applications = os_config.get("applications", [])
                for app in applications:
                    application_actions.append({
                        "type": "install_application",
                        "application": app,
                    })

            elif schema == "preferences":
                # Handle preferences config
                preferences = config.get("_validated_preferences", config.get("preferences", []))

                # Apply OS version filter if specified
                if os_version:
                    if is_unknown_os_version(os_version):
                        raise ValueError(
                            json.dumps({
                                "error": "unknown_os_version",
                                "details": f"unknown version '{os_version}'"
                            })
                        )
                    preferences = filter_preferences_by_os_version(preferences, os_version)

                for pref in preferences:
                    # Skip disabled preferences
                    if not pref.get("enabled", True):
                        continue

                    action = {
                        "type": "set_preference",
                        "name": pref["name"],
                        "domain": pref["domain"],
                        "key": pref["key"],
                        "value": pref["value"],
                        "value_type": pref["value_type"],
                        "apply_command": pref["apply_command"],
                    }

                    # Include optional fields if present
                    if "check_command" in pref:
                        action["check_command"] = pref["check_command"]
                    if "expected_state" in pref:
                        action["expected_state"] = pref["expected_state"]

                    preference_actions.append(action)

            elif schema == "dock":
                # Handle dock config
                # Check OS filter
                if os_filter and config.get("os") != os_filter:
                    continue

                dock_action = {
                    "type": "configure_dock",
                    "items": config["items"],
                }

            elif schema == "environments":
                # Generate environment actions
                env_actions.extend(generate_environment_actions(config))

        # Get file actions from module info
        for file_act in module_info.get("file_actions", []):
            file_actions.append({
                "type": file_act["type"],
                "source": file_act.get("source", ""),
                "destination": file_act.get("destination", ""),
            })

        # Sort actions according to ordering rules
        # 1. install_package, sorted alphabetically by package
        package_actions.sort(key=lambda x: x["package"])

        # 2. install_application, sorted alphabetically by application
        application_actions.sort(key=lambda x: x["application"])

        # file_actions are kept in config order (they're already in order from discovery)

        # Build final action list in order:
        # 1. install_package
        # 2. install_application
        # 3. file actions (link, copy, run)
        # 4. set_preference
        # 5. configure_dock
        # 6. environment actions (install_runtime, install_plugin, create_virtual_env)
        actions = package_actions + application_actions + file_actions + preference_actions

        if dock_action:
            actions.append(dock_action)

        actions.extend(env_actions)

        # Include module if it has actions
        if actions:
            modules_plan.append({
                "name": module_name,
                "actions": actions,
            })

    return {"modules": modules_plan}


def main():
    """Main entry point."""
    args = parse_args()

    try:
        # Discover and validate configs
        configs = discover_configs(
            args.dir,
            os_filter=args.os,
            module_filter=args.modules
        )

        # Handle list-profiles command
        if args.command == "list-profiles":
            # List profiles from manifest
            manifest = configs.get("manifest")
            profiles_list = []

            if manifest:
                # Resolve profiles
                resolved = resolve_profile_inheritance(manifest)
                # Validate modules exist for each profile
                available_modules = set(configs["modules"].keys())
                validate_modules_exist(resolved, available_modules)

                # Build sorted list
                for name in sorted(resolved.keys()):
                    profiles_list.append({
                        "name": name,
                        "modules": sorted(resolved[name])
                    })

            output = {"profiles": profiles_list}
            print(json.dumps(output, indent=2))
            return

        # Handle plan command
        # Check for conflicts (after module filtering)
        conflicts = check_conflicts(configs, args.os, args.os_version, args.dir)
        if conflicts:
            print(json.dumps({"error": "conflict", "conflicts": conflicts}), file=sys.stderr)
            sys.exit(1)

        # Generate plan
        plan = generate_plan(configs, args.os, args.os_version, args.profile, args.modules)

        # Output plan as JSON
        print(json.dumps(plan, indent=2))

    except (ValidationError, ProfileError, ValueError) as e:
        # Handle errors
        if isinstance(e, (ValidationError, ProfileError)):
            if hasattr(e, 'details'):
                error_output = {
                    "error": e.error_type,
                    "details": e.details,
                }
            else:
                error_output = {
                    "error": e.error_type,
                    "details": {"message": str(e)}
                }
        else:
            # Handle ValueError (e.g., unknown_os_version)
            try:
                error_data = json.loads(str(e))
                error_output = error_data
            except json.JSONDecodeError:
                error_output = {
                    "error": "error",
                    "details": {"message": str(e)}
                }

        print(json.dumps(error_output), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
