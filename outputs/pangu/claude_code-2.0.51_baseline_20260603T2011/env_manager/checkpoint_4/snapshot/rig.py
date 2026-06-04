#!/usr/bin/env python3
"""
Rig - Developer Environments and Module Dependencies

Generates JSON execution plans, describing system preferences, Dock layout,
destination conflicts, language runtimes, and virtual environments.
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

import yaml


# ============================================================================
# Constants
# ============================================================================

# OS versions ordered from oldest to newest
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

VALID_VALUE_TYPES = ("bool", "int", "string", "float")

# Track dock configs for global validation
dock_configs_found = []

# Valid schemas
VALID_SCHEMAS = ("module", "preferences", "dock", "environments", "manifest")


# ============================================================================
# Error Types
# ============================================================================

class ErrorType(Enum):
    CIRCULAR_DEPENDENCY = "circular_dependency"
    MISSING_DEPENDENCY = "missing_dependency"
    UNKNOWN_PROFILE = "unknown_profile"
    INVALID_PROFILE = "invalid_profile"
    CIRCULAR_INHERITANCE = "circular_inheritance"
    NO_MANIFEST = "no_manifest"
    MULTIPLE_MANIFESTS = "multiple_manifests"
    MANIFEST_IN_MODULE_DIR = "manifest_in_module_dir"


@dataclass
class ValidationError(Exception):
    """Raised when configuration validation fails."""
    error_type: ErrorType
    message: str
    details: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps({"error": self.error_type.value, **self.details})


# ============================================================================
# Data Structures
# ============================================================================

@dataclass
class VirtualEnvironment:
    version: str
    name: str


@dataclass
class Plugin:
    name: str
    virtual_environments: list[VirtualEnvironment] = field(default_factory=list)


@dataclass
class Environment:
    language: str
    versions: list[str]
    manager: str
    plugins: list[Plugin] = field(default_factory=list)


@dataclass
class Profile:
    name: str
    modules: list[str]
    extends: Optional[str] = None


@dataclass
class Manifest:
    profiles: dict[str, Profile]


@dataclass
class ModuleConfig:
    name: str
    path: Path
    depends_on: list[str] = field(default_factory=list)
    environments: list[Environment] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)
    os_filter: Optional[str] = None
    preferences_path: Optional[Path] = None
    dock_path: Optional[Path] = None


# ============================================================================
# Validation
# ============================================================================

def validate_preferences_config(content: dict, file_path: Path) -> list[str]:
    """Validate a preferences schema configuration."""
    errors = []

    if "preferences" not in content:
        errors.append(f"preferences schema requires 'preferences' list")
        return errors

    prefs = content["preferences"]
    if not isinstance(prefs, list):
        errors.append("preferences: must be a list")
        return errors

    for i, pref in enumerate(prefs):
        if not isinstance(pref, dict):
            errors.append(f"preferences[{i}]: must be an object")
            continue

        if "name" not in pref or not isinstance(pref["name"], str) or not pref["name"]:
            errors.append(f"preferences[{i}].name: required string")

        if "domain" not in pref or not isinstance(pref["domain"], str) or not pref["domain"]:
            errors.append(f"preferences[{i}].domain: required string")

        if "key" not in pref or not isinstance(pref["key"], str) or not pref["key"]:
            errors.append(f"preferences[{i}].key: required string")

        if "value" not in pref:
            errors.append(f"preferences[{i}].value: required")

        if "value_type" not in pref:
            errors.append(f"preferences[{i}].value_type: required")
        elif not isinstance(pref["value_type"], str):
            errors.append(f"preferences[{i}].value_type: must be a string")
        elif pref["value_type"] not in VALID_VALUE_TYPES:
            errors.append(
                f"preferences[{i}].value_type: must be 'bool', 'int', 'string', or 'float', got '{pref['value_type']}'"
            )

        if "apply_command" not in pref or not isinstance(pref["apply_command"], str) or not pref["apply_command"]:
            errors.append(f"preferences[{i}].apply_command: required string")

        if "check_command" in pref and not isinstance(pref["check_command"], str):
            errors.append(f"preferences[{i}].check_command: must be a string")

        if "expected_state" in pref and not isinstance(pref["expected_state"], str):
            errors.append(f"preferences[{i}].expected_state: must be a string")

        if "enabled" in pref and not isinstance(pref["enabled"], bool):
            errors.append(f"preferences[{i}].enabled: must be a boolean")

        if "min_version" in pref:
            if not isinstance(pref["min_version"], str):
                errors.append(f"preferences[{i}].min_version: must be a string")
            elif pref["min_version"] not in OS_VERSIONS:
                errors.append(f"preferences[{i}].min_version: unknown version '{pref['min_version']}'")

        if "max_version" in pref:
            if not isinstance(pref["max_version"], str):
                errors.append(f"preferences[{i}].max_version: must be a string")
            elif pref["max_version"] not in OS_VERSIONS:
                errors.append(f"preferences[{i}].max_version: unknown version '{pref['max_version']}'")

        if "min_version" in pref and "max_version" in pref:
            min_v = pref["min_version"]
            max_v = pref["max_version"]
            if min_v in OS_VERSIONS and max_v in OS_VERSIONS:
                min_idx = OS_VERSIONS.index(min_v)
                max_idx = OS_VERSIONS.index(max_v)
                if min_idx > max_idx:
                    errors.append(f"preferences[{i}]: min_version '{min_v}' is later than max_version '{max_v}'")

    return errors


def validate_dock_config(content: dict, file_path: Path) -> list[str]:
    """Validate a dock schema configuration."""
    errors = []

    if "os" not in content or content["os"] != "macos":
        errors.append("dock: 'os' is required and must be 'macos'")

    if "items" not in content:
        errors.append("dock.items: must be a non-empty list of strings")
    else:
        items = content["items"]
        if not isinstance(items, list):
            errors.append("dock.items: must be a non-empty list of strings")
        elif not items:
            errors.append("dock.items: must be a non-empty list of strings")
        elif not all(isinstance(item, str) for item in items):
            errors.append("dock.items: must be a non-empty list of strings")

    return errors


def validate_module_config(content: dict, file_path: Path) -> list[str]:
    """Validate a module schema configuration."""
    errors = []

    if "os" in content:
        os_val = content["os"]
        if not isinstance(os_val, dict):
            errors.append("os: must be an object")
        else:
            if "name" not in os_val:
                errors.append("os.name: missing")
            elif not isinstance(os_val["name"], str):
                errors.append("os.name: must be a string")
            elif os_val["name"] not in ("macos", "linux"):
                errors.append('os.name: must be "macos" or "linux"')

            if "package_manager" in os_val:
                pm = os_val["package_manager"]
                if not isinstance(pm, str):
                    errors.append("os.package_manager: must be a string")
                elif pm not in ("brew", "apt", "yum"):
                    errors.append('os.package_manager: must be "brew", "apt", or "yum"')

            has_packages = "packages" in os_val
            has_apps = "applications" in os_val
            if has_packages or has_apps:
                if "package_manager" not in os_val:
                    errors.append("os.package_manager: required when packages or applications present")
                elif isinstance(os_val.get("package_manager"), str) and os_val["package_manager"] not in ("brew", "apt", "yum"):
                    pass

            if "packages" in os_val:
                packages = os_val["packages"]
                if not isinstance(packages, list):
                    errors.append("os.packages: must be a list")
                else:
                    for i, pkg in enumerate(packages):
                        if not isinstance(pkg, str):
                            errors.append(f"os.packages[{i}]: must be a string")
                        elif not pkg:
                            errors.append(f"os.packages[{i}]: must be non-empty")

            if "applications" in os_val:
                apps = os_val["applications"]
                if not isinstance(apps, list):
                    errors.append("os.applications: must be a list")
                else:
                    for i, app in enumerate(apps):
                        if not isinstance(app, str):
                            errors.append(f"os.applications[{i}]: must be a string")
                        elif not app:
                            errors.append(f"os.applications[{i}]: must be non-empty")

    if "depends_on" in content:
        depends_on = content["depends_on"]
        if not isinstance(depends_on, list):
            errors.append("depends_on: must be a list")
        else:
            for i, dep in enumerate(depends_on):
                if not isinstance(dep, str):
                    errors.append(f"depends_on[{i}]: must be a string")
                elif not dep:
                    errors.append(f"depends_on[{i}]: must be non-empty")

    if "actions" in content:
        actions = content["actions"]
        if not isinstance(actions, list):
            errors.append("actions: must be a list")
        else:
            for i, action in enumerate(actions):
                if not isinstance(action, dict):
                    errors.append(f"actions[{i}]: must be an object")
                    continue

                if "type" not in action:
                    errors.append(f"actions[{i}].type: missing")
                elif not isinstance(action["type"], str):
                    errors.append(f"actions[{i}].type: must be a string")
                elif action["type"] not in ("link", "copy", "run"):
                    errors.append(f"actions[{i}].type: must be 'link', 'copy', or 'run'")

                if "source" not in action:
                    errors.append(f"actions[{i}].source: missing")
                elif not isinstance(action["source"], str):
                    errors.append(f"actions[{i}].source: must be a string")
                elif not action["source"]:
                    errors.append(f"actions[{i}].source: must be non-empty")
                elif action["source"].startswith("/"):
                    errors.append(f"actions[{i}].source: must not start with /")

                if "destination" in action:
                    dest = action["destination"]
                    if not isinstance(dest, str):
                        errors.append(f"actions[{i}].destination: must be a string")
                    elif not dest:
                        errors.append(f"actions[{i}].destination: must be non-empty")
                    elif not (dest.startswith("/") or dest.startswith("~")):
                        errors.append(f"actions[{i}].destination: must start with / or ~")
                    elif action.get("source") == "*" and action.get("type") in ("link", "copy"):
                        if not dest.endswith("/"):
                            errors.append(f"actions[{i}].destination: must end with / for wildcard")

                if "hidden" in action and not isinstance(action["hidden"], bool):
                    errors.append(f"actions[{i}].hidden: must be a boolean")

                if "elevated" in action and not isinstance(action["elevated"], bool):
                    errors.append(f"actions[{i}].elevated: must be a boolean")

    return errors


def validate_environments_config(content: dict, file_path: Path) -> list[str]:
    """Validate an environments schema configuration."""
    errors = []

    if "environments" not in content or not isinstance(content["environments"], list):
        errors.append(f"{file_path}: 'environments' is required and must be a non-empty list")
        return errors

    if not content["environments"]:
        errors.append(f"{file_path}: 'environments' must not be empty")
        return errors

    for i, env_data in enumerate(content["environments"]):
        # language
        if "language" not in env_data or not env_data["language"]:
            errors.append(f"{file_path}: environments[{i}].language is required and must be a non-empty string")
        language = env_data.get("language", "")

        # versions
        if "versions" not in env_data or not isinstance(env_data["versions"], list):
            errors.append(f"{file_path}: environments[{i}].versions is required and must be a non-empty list")
        else:
            versions = env_data["versions"]
            if not versions:
                errors.append(f"{file_path}: environments[{i}].versions must be a non-empty list")
            elif not all(isinstance(v, str) and v for v in versions):
                errors.append(f"{file_path}: environments[{i}].versions must contain non-empty strings")

        # manager
        if "manager" not in env_data or not isinstance(env_data["manager"], dict):
            errors.append(f"{file_path}: environments[{i}].manager is required and must be an object")
        else:
            manager = env_data["manager"]
            if "name" not in manager or not manager["name"]:
                errors.append(f"{file_path}: environments[{i}].manager.name is required and must be non-empty")

            # plugins
            if "plugins" in manager and manager["plugins"]:
                if not isinstance(manager["plugins"], list):
                    errors.append(f"{file_path}: environments[{i}].manager.plugins must be a list")
                else:
                    for j, plugin in enumerate(manager["plugins"]):
                        if "name" not in plugin or not plugin["name"]:
                            errors.append(f"{file_path}: environments[{i}].manager.plugins[{j}].name is required")

                        if "virtual_environments" in plugin and plugin["virtual_environments"]:
                            if not isinstance(plugin["virtual_environments"], list):
                                errors.append(f"{file_path}: environments[{i}].manager.plugins[{j}].virtual_environments must be a list")
                            else:
                                for k, ve in enumerate(plugin["virtual_environments"]):
                                    if "version" not in ve:
                                        errors.append(f"{file_path}: environments[{i}].manager.plugins[{j}].virtual_environments[{k}].version is required")
                                    elif language and "versions" in env_data and ve["version"] not in env_data["versions"]:
                                        errors.append(f"{file_path}: environments[{i}].manager.plugins[{j}].virtual_environments[{k}].version: '{ve['version']}' is not in the environment's versions list")

                                    if "name" not in ve or not ve["name"]:
                                        errors.append(f"{file_path}: environments[{i}].manager.plugins[{j}].virtual_environments[{k}].name is required")

    return errors


def validate_manifest_config(content: dict, file_path: Path, all_profile_names: set[str]) -> list[str]:
    """Validate a manifest schema configuration."""
    errors = []

    if "profiles" not in content:
        errors.append("manifest: 'profiles' is required")
        return errors

    profiles = content["profiles"]
    if not isinstance(profiles, dict):
        errors.append("profiles: must be an object")
        return errors

    if not profiles:
        errors.append("profiles: must not be empty")
        return errors

    for name, profile_data in profiles.items():
        if not isinstance(profile_data, dict):
            errors.append(f"profiles.{name}: must be an object")
            continue

        # Check modules
        if "modules" not in profile_data:
            errors.append(f"profiles.{name}.modules: required")
            continue

        modules = profile_data["modules"]
        if not isinstance(modules, list):
            errors.append(f"profiles.{name}.modules: must be a list")
            continue

        if not modules:
            errors.append(f"profiles.{name}.modules: must be a non-empty list")
            continue

        for i, mod in enumerate(modules):
            if not isinstance(mod, str):
                errors.append(f"profiles.{name}.modules[{i}]: must be a string")
            elif not mod:
                errors.append(f"profiles.{name}.modules[{i}]: must be non-empty")

        # Check extends
        if "extends" in profile_data:
            extends = profile_data["extends"]
            if not isinstance(extends, str):
                errors.append(f"profiles.{name}.extends: must be a string")
            elif not extends:
                errors.append(f"profiles.{name}.extends: must be non-empty")
            elif extends not in all_profile_names:
                errors.append(f"profiles.{name}.extends: unknown profile '{extends}'")

    return errors


def validate_config(content: dict, file_path: Path) -> list[str]:
    """Validate a configuration file."""
    errors = []

    if "version" not in content:
        errors.append("version: missing")
    else:
        version = content["version"]
        if not isinstance(version, str):
            errors.append("version: must be a string")
        elif version != "1":
            errors.append(f"unsupported version '{version}'")

    if "schema" not in content:
        if "version" in content and isinstance(content["version"], str) and content["version"] == "1":
            errors.append("schema: missing")
    else:
        schema = content["schema"]
        if not isinstance(schema, str):
            errors.append("schema: must be a string")
        elif schema not in VALID_SCHEMAS:
            errors.append(f"schema: unrecognized schema '{schema}'")

    if "version" in content:
        if not isinstance(content["version"], str):
            return errors
        if content["version"] != "1":
            return errors

    schema = content.get("schema", "")

    if schema == "preferences":
        errors.extend(validate_preferences_config(content, file_path))
    elif schema == "dock":
        errors.extend(validate_dock_config(content, file_path))
        dock_configs_found.append(file_path)
    elif schema == "module":
        errors.extend(validate_module_config(content, file_path))
    elif schema == "environments":
        errors.extend(validate_environments_config(content, file_path))
    elif schema == "manifest":
        # For validation, we can't know all profile names in advance
        # So we do basic validation without extends checking
        if "profiles" not in content:
            errors.append("manifest: 'profiles' is required")
        elif not isinstance(content["profiles"], dict):
            errors.append("profiles: must be an object")
        elif not content["profiles"]:
            errors.append("profiles: must not be empty")
        else:
            for name, profile_data in content["profiles"].items():
                if not isinstance(profile_data, dict):
                    errors.append(f"profiles.{name}: must be an object")
                    continue

                if "modules" not in profile_data:
                    errors.append(f"profiles.{name}.modules: required")
                    continue

                modules = profile_data["modules"]
                if not isinstance(modules, list):
                    errors.append(f"profiles.{name}.modules: must be a list")
                elif not modules:
                    errors.append(f"profiles.{name}.modules: must be a non-empty list")
                else:
                    for i, mod in enumerate(modules):
                        if not isinstance(mod, str):
                            errors.append(f"profiles.{name}.modules[{i}]: must be a string")
                        elif not mod:
                            errors.append(f"profiles.{name}.modules[{i}]: must be non-empty")

                if "extends" in profile_data:
                    extends = profile_data["extends"]
                    if not isinstance(extends, str):
                        errors.append(f"profiles.{name}.extends: must be a string")
                    elif not extends:
                        errors.append(f"profiles.{name}.extends: must be non-empty")

    return errors


def parse_yaml_file(file_path: Path) -> Optional[dict]:
    """Parse a YAML file and return its contents."""
    try:
        with open(file_path, 'r') as f:
            return yaml.safe_load(f)
    except (yaml.YAMLError, IOError):
        return None


# ============================================================================
# Config Discovery
# ============================================================================

# ============================================================================
# Manifest Discovery and Parsing
# ============================================================================

manifest_file_path: Optional[Path] = None


def discover_manifest(root_dir: Path) -> Optional[tuple[Path, dict]]:
    """Find exactly one manifest.yaml file in the root directory."""
    candidates = list(root_dir.glob("*.yaml"))
    manifests = []

    for yaml_file in candidates:
        content = parse_yaml_file(yaml_file)
        if content and content.get("schema") == "manifest":
            manifests.append((yaml_file, content))

    if len(manifests) == 0:
        return None
    if len(manifests) > 1:
        raise ValidationError(
            ErrorType.MULTIPLE_MANIFESTS,
            "multiple 'manifest' schemas found — only one is allowed"
        )

    return manifests[0]


def validate_manifest_placement(manifest_path: Path, root_dir: Path) -> None:
    """Ensure manifest is in the root directory, not in a module directory."""
    try:
        relative = manifest_path.relative_to(root_dir)
    except ValueError:
        return

    # If manifest is in a subdirectory (not just root), it's in a module dir
    parts = relative.parent.parts
    if len(parts) > 1 or (len(parts) == 1 and parts[0] != "."):
        module_name = parts[0] if parts else manifest_path.stem
        raise ValidationError(
            ErrorType.MANIFEST_IN_MODULE_DIR,
            f"manifest schema must be in the root directory, found in '{module_name}'"
        )


def parse_manifest(manifest_data: tuple[Path, dict], root_dir: Path) -> Manifest:
    """Parse manifest data into a Manifest object."""
    manifest_path, content = manifest_data

    validate_manifest_placement(manifest_path, root_dir)

    if "profiles" not in content or not isinstance(content["profiles"], dict) or not content["profiles"]:
        raise ValidationError(
            ErrorType.INVALID_PROFILE,
            "profiles must be a non-empty object"
        )

    profiles = {}
    for name, profile_data in content["profiles"].items():
        if not isinstance(profile_data, dict):
            raise ValidationError(
                ErrorType.INVALID_PROFILE,
                f"profiles.{name}: must be an object"
            )

        # Check modules
        if "modules" not in profile_data:
            raise ValidationError(
                ErrorType.INVALID_PROFILE,
                f"profiles.{name}.modules: required"
            )

        modules = profile_data["modules"]
        if not isinstance(modules, list) or not modules:
            raise ValidationError(
                ErrorType.INVALID_PROFILE,
                f"profiles.{name}.modules: must be a non-empty list"
            )

        # Validate each module entry
        for i, mod in enumerate(modules):
            if not isinstance(mod, str) or not mod:
                raise ValidationError(
                    ErrorType.INVALID_PROFILE,
                    f"profiles.{name}.modules[{i}]: must be a non-empty string"
                )

        extends = profile_data.get("extends")
        if extends is not None:
            if not isinstance(extends, str) or not extends:
                raise ValidationError(
                    ErrorType.INVALID_PROFILE,
                    f"profiles.{name}.extends: must be a non-empty string"
                )

        profiles[name] = Profile(
            name=name,
            modules=modules,
            extends=extends
        )

    return Manifest(profiles=profiles)


# ============================================================================
# Profile Resolution
# ============================================================================

def resolve_profile(manifest: Manifest, profile_name: str) -> list[str]:
    """Resolve a profile to its final module list, handling inheritance.

    Returns a sorted list of unique module names.
    """
    if profile_name not in manifest.profiles:
        raise ValidationError(
            ErrorType.UNKNOWN_PROFILE,
            f"unknown profile '{profile_name}'",
            {"details": f"unknown profile '{profile_name}'"}
        )

    # Check for circular inheritance using DFS
    visited = set()
    rec_stack = set()
    cycle = []

    def detect_inheritance_cycle(profile_name: str, path: list[str]) -> bool:
        nonlocal cycle
        if profile_name in rec_stack:
            # Found a cycle
            idx = path.index(profile_name)
            cycle = path[idx:] + [profile_name]
            return True
        if profile_name in visited:
            return False

        visited.add(profile_name)
        rec_stack.add(profile_name)
        path.append(profile_name)

        profile = manifest.profiles[profile_name]
        if profile.extends:
            if detect_inheritance_cycle(profile.extends, path):
                return True

        rec_stack.remove(profile_name)
        path.pop()
        return False

    if detect_inheritance_cycle(profile_name, []):
        raise ValidationError(
            ErrorType.CIRCULAR_INHERITANCE,
            "circular inheritance detected",
            {"cycle": cycle}
        )

    # Reset for resolution
    visited = set()
    rec_stack = set()

    def resolve_recursive(name: str, resolved: list[str], seen: set[str]) -> None:
        """Recursively resolve profile inheritance."""
        if name in seen:
            return

        profile = manifest.profiles[name]
        seen.add(name)

        # Process parent first
        if profile.extends:
            resolve_recursive(profile.extends, resolved, seen)

        # Add own modules
        for module in profile.modules:
            if module not in resolved:
                resolved.append(module)

    final_modules = []
    seen_profiles = set()
    resolve_recursive(profile_name, final_modules, seen_profiles)

    # Deduplicate while preserving order
    seen_modules = set()
    unique_modules = []
    for module in final_modules:
        if module not in seen_modules:
            seen_modules.add(module)
            unique_modules.append(module)

    return sorted(unique_modules)


def resolve_all_profiles(manifest: Manifest) -> dict[str, list[str]]:
    """Resolve all profiles in the manifest."""
    result = {}
    for name in sorted(manifest.profiles.keys()):
        result[name] = resolve_profile(manifest, name)
    return result


def discover_configs(root_dir: Path) -> list[tuple[Path, dict]]:
    """Discover all YAML config files in a directory tree."""
    configs = []
    for yaml_file in sorted(root_dir.rglob("*.yaml")):
        content = parse_yaml_file(yaml_file)
        if content and "schema" in content:
            configs.append((yaml_file, content))
    return configs


def parse_module_configs(configs: list[tuple[Path, dict]], base_dir: Path, os_filter: Optional[str] = None) -> dict[str, ModuleConfig]:
    """Parse and organize all configs into ModuleConfig objects."""
    modules: dict[str, ModuleConfig] = {}

    for file_path, content in configs:
        module_name = file_path.parent.relative_to(base_dir).as_posix()
        schema = content.get("schema")

        if module_name not in modules:
            modules[module_name] = ModuleConfig(
                name=module_name,
                path=file_path.parent
            )

        module = modules[module_name]

        if schema == "module":
            # depends_on
            if "depends_on" in content:
                module.depends_on = [d for d in content["depends_on"] if d]

            # os filter
            if "os" in content and isinstance(content["os"], dict):
                module.os_filter = content["os"].get("name")

            # actions
            module.actions = content.get("actions", [])

        elif schema == "environments":
            # Parse environments
            for env_data in content.get("environments", []):
                environment = Environment(
                    language=env_data["language"],
                    versions=env_data["versions"],
                    manager=env_data["manager"]["name"]
                )

                for plugin_data in env_data["manager"].get("plugins", []):
                    plugin = Plugin(name=plugin_data["name"])
                    for ve_data in plugin_data.get("virtual_environments", []):
                        plugin.virtual_environments.append(
                            VirtualEnvironment(
                                version=ve_data["version"],
                                name=ve_data["name"]
                            )
                        )
                    environment.plugins.append(plugin)

                module.environments.append(environment)

        elif schema == "preferences":
            module.preferences_path = file_path

        elif schema == "dock":
            module.dock_path = file_path

    # Apply OS filter
    if os_filter:
        filtered = {}
        for name, module in modules.items():
            if module.os_filter is None or module.os_filter == os_filter:
                filtered[name] = module
        modules = filtered

    return modules


# ============================================================================
# Dependency Resolution
# ============================================================================

def find_missing_dependencies(modules: dict[str, ModuleConfig]) -> dict[str, list[str]]:
    """Find modules with missing dependencies."""
    module_names = set(modules.keys())
    missing = {}

    for name, module in modules.items():
        for dep in module.depends_on:
            if dep not in module_names:
                if name not in missing:
                    missing[name] = []
                missing[name].append(dep)

    return missing


def detect_cycle(modules: dict[str, ModuleConfig]) -> Optional[list[str]]:
    """Detect circular dependencies using DFS."""
    visited = set()
    rec_stack = set()
    cycle = []

    def dfs(node: str) -> bool:
        nonlocal cycle
        visited.add(node)
        rec_stack.add(node)

        module = modules.get(node)
        if module:
            for neighbor in module.depends_on:
                if neighbor not in modules:
                    continue
                if neighbor not in visited:
                    if dfs(neighbor):
                        return True
                elif neighbor in rec_stack:
                    cycle = [neighbor]
                    temp = node
                    while temp != neighbor:
                        cycle.append(temp)
                        for m_name, m in modules.items():
                            if temp in m.depends_on:
                                temp = m_name
                                break
                    cycle.append(neighbor)
                    cycle.reverse()
                    return True

        rec_stack.remove(node)
        return False

    for name in modules:
        if name not in visited:
            if dfs(name):
                return cycle

    return None


def topological_sort(modules: dict[str, ModuleConfig]) -> list[ModuleConfig]:
    """Perform topological sort on modules."""
    module_names = list(modules.keys())
    in_degree = {name: 0 for name in module_names}
    graph = {name: [] for name in module_names}

    for name in module_names:
        module = modules[name]
        for dep in module.depends_on:
            if dep in modules:
                graph[dep].append(name)
                in_degree[name] += 1

    queue = [name for name, degree in in_degree.items() if degree == 0]
    queue.sort()

    result = []
    while queue:
        current = queue.pop(0)
        result.append(modules[current])

        for neighbor in sorted(graph[current]):
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)
                queue.sort()

    if len(result) != len(modules):
        cycle = detect_cycle(modules)
        if cycle:
            raise ValidationError(
                ErrorType.CIRCULAR_DEPENDENCY,
                "Circular dependency detected",
                {"cycle": cycle}
            )

    return result


# ============================================================================
# Action Generation
# ============================================================================

def generate_preference_actions(module: ModuleConfig, os_version_filter: Optional[str]) -> list[dict]:
    """Generate set_preference actions from preferences config."""
    actions = []

    if not module.preferences_path:
        return actions

    prefs_content = parse_yaml_file(module.preferences_path)
    if not prefs_content:
        return actions

    for pref in prefs_content.get("preferences", []):
        if not pref.get("enabled", True):
            continue

        action = {
            "type": "set_preference",
            "name": pref["name"],
            "domain": pref["domain"],
            "key": pref["key"],
            "value": pref["value"],
            "value_type": pref["value_type"],
            "apply_command": pref["apply_command"]
        }

        if "check_command" in pref:
            action["check_command"] = pref["check_command"]
        if "expected_state" in pref:
            action["expected_state"] = pref["expected_state"]

        actions.append(action)

    return actions


def generate_dock_action(module: ModuleConfig, os_filter: Optional[str]) -> Optional[dict]:
    """Generate configure_dock action if OS filter matches."""
    if not module.dock_path:
        return None

    if os_filter is not None and os_filter != "macos":
        return None

    dock_content = parse_yaml_file(module.dock_path)
    if not dock_content:
        return None

    return {
        "type": "configure_dock",
        "items": dock_content.get("items", [])
    }


def generate_file_actions(module: ModuleConfig) -> list[dict]:
    """Generate file actions (link, copy, run) from module config."""
    actions = []

    for action in module.actions:
        act_type = action["type"]
        source = action["source"]
        elevated = action.get("elevated", False)

        if source == "*":
            for item in sorted(module.path.iterdir()):
                if item.is_dir():
                    continue
                if item.name.startswith("."):
                    continue
                if item.suffix.lower() in (".yaml", ".yml", ".json"):
                    continue

                filename = item.name
                dest = action["destination"] + filename

                if action.get("hidden", False):
                    dest_parts = dest.rsplit("/", 1)
                    if len(dest_parts) == 2:
                        dir_part, file_part = dest_parts
                        if not file_part.startswith("."):
                            file_part = "." + file_part
                        dest = dir_part + "/" + file_part
                    else:
                        if not dest.startswith("."):
                            dest = "." + dest

                if dest.startswith("~"):
                    dest = str(Path.home()) + dest[1:]

                if act_type in ("link", "copy"):
                    actions.append({
                        "type": act_type,
                        "source": str(item),
                        "destination": dest,
                        "elevated": elevated
                    })
        else:
            src_path = module.path / source
            src_path_str = str(src_path) if not src_path.is_absolute() else str(src_path)

            if act_type == "run":
                actions.append({
                    "type": "run",
                    "source": src_path_str,
                    "elevated": elevated
                })
            else:
                dest = action["destination"]

                if action.get("hidden", False):
                    dest_parts = dest.rsplit("/", 1)
                    if len(dest_parts) == 2:
                        dir_part, file_part = dest_parts
                        if not file_part.startswith("."):
                            file_part = "." + file_part
                        dest = dir_part + "/" + file_part
                    else:
                        if not dest.startswith("."):
                            dest = "." + dest

                if dest.startswith("~"):
                    dest = str(Path.home()) + dest[1:]

                actions.append({
                    "type": act_type,
                    "source": src_path_str,
                    "destination": dest,
                    "elevated": elevated
                })

    return actions


def generate_package_actions(module: ModuleConfig) -> list[dict]:
    """Generate install_package actions from module config."""
    actions = []

    os_config = None
    for _, content in discover_configs(module.path):
        if content.get("schema") == "module" and "os" in content:
            os_config = content["os"]
            break

    if not os_config or "packages" not in os_config:
        return actions

    pm = os_config.get("package_manager", "")
    for pkg in os_config["packages"]:
        actions.append({
            "type": "install_package",
            "manager": pm,
            "package": pkg
        })

    return actions


def generate_application_actions(module: ModuleConfig) -> list[dict]:
    """Generate install_application actions from module config."""
    actions = []

    os_config = None
    for _, content in discover_configs(module.path):
        if content.get("schema") == "module" and "os" in content:
            os_config = content["os"]
            break

    if not os_config or "applications" not in os_config:
        return actions

    pm = os_config.get("package_manager", "")
    for app in os_config["applications"]:
        actions.append({
            "type": "install_application",
            "manager": pm,
            "application": app
        })

    return actions


def generate_environment_actions(module: ModuleConfig) -> list[dict]:
    """Generate environment actions for a module."""
    actions = []

    for env in module.environments:
        # Install runtimes for each version
        for version in env.versions:
            actions.append({
                "type": "install_runtime",
                "language": env.language,
                "version": version,
                "manager": env.manager
            })

        # Install plugins
        for plugin in env.plugins:
            actions.append({
                "type": "install_plugin",
                "manager": env.manager,
                "plugin": plugin.name
            })

        # Create virtual environments (grouped by plugin, in order)
        for plugin in env.plugins:
            for ve in plugin.virtual_environments:
                actions.append({
                    "type": "create_virtual_env",
                    "language": env.language,
                    "manager": env.manager,
                    "plugin": plugin.name,
                    "version": ve.version,
                    "name": ve.name
                })

    return actions


def sort_actions(actions: list[dict]) -> list[dict]:
    """Sort actions according to ordering rules."""
    pkg_actions = [a for a in actions if a["type"] == "install_package"]
    app_actions = [a for a in actions if a["type"] == "install_application"]
    file_actions = [a for a in actions if a["type"] in ("link", "copy", "run")]
    pref_actions = [a for a in actions if a["type"] == "set_preference"]
    dock_actions = [a for a in actions if a["type"] == "configure_dock"]
    env_actions = [a for a in actions if a["type"] in ("install_runtime", "install_plugin", "create_virtual_env")]

    pkg_actions.sort(key=lambda a: a["package"])
    app_actions.sort(key=lambda a: a["application"])

    return pkg_actions + app_actions + file_actions + pref_actions + dock_actions + env_actions


def generate_module_plan(module: ModuleConfig, os_filter: Optional[str], os_version_filter: Optional[str]) -> dict:
    """Generate the complete action plan for a module."""
    all_actions = []

    # File actions first
    all_actions.extend(generate_file_actions(module))

    # Package actions
    all_actions.extend(generate_package_actions(module))

    # Application actions
    all_actions.extend(generate_application_actions(module))

    # Preference actions
    all_actions.extend(generate_preference_actions(module, os_version_filter))

    # Dock action
    dock_action = generate_dock_action(module, os_filter)
    if dock_action:
        all_actions.append(dock_action)

    # Environment actions last (after all file/preference/dock actions)
    all_actions.extend(generate_environment_actions(module))

    # Sort actions
    all_actions = sort_actions(all_actions)

    return {
        "name": module.name,
        "actions": all_actions
    }


def detect_conflicts(modules_data: dict[str, dict]) -> Optional[dict]:
    """Detect conflicts where multiple file actions write to the same destination."""
    dest_map: dict[str, list[dict]] = {}

    for module_name, module_data in modules_data.items():
        for action in module_data.get("actions", []):
            if action["type"] in ("link", "copy"):
                dest = action["destination"]
                if dest not in dest_map:
                    dest_map[dest] = []
                dest_map[dest].append({
                    "module": module_name,
                    "type": action["type"],
                    "source": action["source"]
                })

    conflicts = []
    for dest in sorted(dest_map.keys()):
        sources = dest_map[dest]
        if len(sources) > 1:
            sources.sort(key=lambda s: (s["module"], s["source"], s["type"]))
            conflicts.append({
                "destination": dest,
                "sources": sources
            })

    if not conflicts:
        return None

    return {
        "error": "conflict",
        "conflicts": conflicts
    }


# ============================================================================
# CLI Commands
# ============================================================================

def validate_command(base_dir: str) -> int:
    """Implement the 'validate' command."""
    global dock_configs_found

    base_path = Path(base_dir)
    if not base_path.exists():
        print(json.dumps({"valid": False, "errors": ["directory not found"]}), file=sys.stderr)
        return 1

    configs = discover_configs(base_path)
    results = []
    all_valid = True

    dock_configs_found = []

    for file_path, content in configs:
        errors = validate_config(content, file_path)
        valid = len(errors) == 0
        if not valid:
            all_valid = False

        file_result = {
            "path": str(file_path.relative_to(base_path)),
            "valid": valid
        }
        if not valid:
            file_result["errors"] = errors

        results.append(file_result)

    if len(dock_configs_found) > 1:
        all_valid = False
        dock_files = [str(f.relative_to(base_path)) for f in dock_configs_found]
        for dock_file in dock_configs_found:
            results.append({
                "path": str(dock_file.relative_to(base_path)),
                "valid": False,
                "errors": [f"multiple 'dock' schemas found - only one is allowed (found in: {', '.join(dock_files)})"]
            })

    results.sort(key=lambda x: x["path"])

    output = {
        "valid": all_valid,
        "files": results
    }
    print(json.dumps(output, indent=2))
    return 0 if all_valid else 1


def check_invalid_profiles(manifest: Manifest, available_modules: set[str]) -> list[tuple[str, list[str]]]:
    """Check all resolved profiles for missing modules."""
    invalid = []

    for name, profile in manifest.profiles.items():
        try:
            resolved = resolve_profile(manifest, name)
            missing = [m for m in resolved if m not in available_modules]
            if missing:
                missing.sort()
                invalid.append((name, missing))
        except ValidationError:
            # Circular inheritance will be caught later
            pass

    return invalid


def list_profiles_command(base_dir: str) -> int:
    """Implement the 'list-profiles' command."""
    base_path = Path(base_dir)
    if not base_path.exists():
        print(json.dumps({"profiles": []}), file=sys.stderr)
        return 1

    # Try to find manifest
    try:
        manifest_data = discover_manifest(base_path)
    except ValidationError as e:
        print(json.dumps({"profiles": []}), file=sys.stderr)
        return 1

    if not manifest_data:
        print(json.dumps({"profiles": []}))
        return 0

    try:
        manifest = parse_manifest(manifest_data, base_path)
    except ValidationError as e:
        # Check for invalid profiles
        configs = discover_configs(base_path)
        modules = parse_module_configs(configs, base_path)

        # Try to get missing modules info from error
        error_msg = str(e)

        # Check for invalid profiles with missing modules
        invalid_list = check_invalid_profiles(manifest, set(modules.keys()))
        if invalid_list:
            profile_name, missing_modules = invalid_list[0]  # alphabetically first
            print(json.dumps({
                "error": "invalid_profile",
                "profile": profile_name,
                "missing_modules": missing_modules
            }, indent=2), file=sys.stderr)
            return 1

        # Check for circular inheritance
        cycle = detect_inheritance_cycle_in_manifest(manifest)
        if cycle:
            print(json.dumps({
                "error": "circular_inheritance",
                "cycle": cycle
            }, indent=2), file=sys.stderr)
            return 1

        # Unknown profile or other validation error
        print(json.dumps({"error": e.error_type.value, "details": error_msg}, indent=2), file=sys.stderr)
        return 1

    # Get available modules
    configs = discover_configs(base_path)
    modules = parse_module_configs(configs, base_path)

    # Check for invalid profiles
    invalid_list = check_invalid_profiles(manifest, set(modules.keys()))
    if invalid_list:
        profile_name, missing_modules = invalid_list[0]  # alphabetically first
        print(json.dumps({
            "error": "invalid_profile",
            "profile": profile_name,
            "missing_modules": missing_modules
        }, indent=2), file=sys.stderr)
        return 1

    # Check for circular inheritance
    cycle = detect_inheritance_cycle_in_manifest(manifest)
    if cycle:
        print(json.dumps({
            "error": "circular_inheritance",
            "cycle": cycle
        }, indent=2), file=sys.stderr)
        return 1

    # Resolve and output all profiles
    try:
        resolved = resolve_all_profiles(manifest)
        output = {"profiles": []}
        for name in sorted(resolved.keys()):
            output["profiles"].append({
                "name": name,
                "modules": resolved[name]
            })
        print(json.dumps(output, indent=2))
        return 0
    except ValidationError as e:
        if e.error_type == ErrorType.CIRCULAR_INHERITANCE:
            print(json.dumps({
                "error": "circular_inheritance",
                "cycle": e.details.get("cycle", [])
            }, indent=2), file=sys.stderr)
            return 1
        raise


def detect_inheritance_cycle_in_manifest(manifest: Manifest) -> list[str]:
    """Detect circular inheritance in manifest."""
    visited = set()
    rec_stack = set()
    cycle = []

    def dfs(name: str, path: list[str]) -> bool:
        nonlocal cycle
        if name in rec_stack:
            idx = path.index(name)
            cycle = path[idx:] + [name]
            return True
        if name in visited:
            return False

        visited.add(name)
        rec_stack.add(name)
        path.append(name)

        profile = manifest.profiles[name]
        if profile.extends:
            if dfs(profile.extends, path):
                return True

        rec_stack.remove(name)
        path.pop()
        return False

    for name in sorted(manifest.profiles.keys()):
        if name not in visited:
            if dfs(name, []):
                return cycle

    return []


def plan_command(base_dir: str, os_filter: Optional[str], os_version_filter: Optional[str], module_filter: Optional[list[str]], profile_name: Optional[str] = None) -> int:
    """Implement the 'plan' command."""
    global dock_configs_found

    base_path = Path(base_dir)
    if not base_path.exists():
        print(json.dumps({"error": "directory not found"}), file=sys.stderr)
        return 1

    # Try to find manifest
    manifest_data = discover_manifest(base_path)
    manifest = None

    if manifest_data:
        try:
            manifest = parse_manifest(manifest_data, base_path)
        except ValidationError as e:
            if profile_name:
                print(json.dumps({"error": "no_manifest", "details": "no manifest found but --profile was specified"}, indent=2), file=sys.stderr)
                return 1
            # Otherwise continue without manifest

    # Handle --profile flag
    if profile_name:
        if not manifest:
            print(json.dumps({"error": "no_manifest", "details": "no manifest found but --profile was specified"}, indent=2), file=sys.stderr)
            return 1

        try:
            resolved_profile_modules = resolve_profile(manifest, profile_name)
        except ValidationError as e:
            if e.error_type == ErrorType.UNKNOWN_PROFILE:
                print(json.dumps({
                    "error": "unknown_profile",
                    "details": f"unknown profile '{profile_name}'"
                }, indent=2), file=sys.stderr)
                return 1
            elif e.error_type == ErrorType.CIRCULAR_INHERITANCE:
                print(json.dumps({
                    "error": "circular_inheritance",
                    "cycle": e.details.get("cycle", [])
                }, indent=2), file=sys.stderr)
                return 1
            raise

        # Check for invalid profiles (missing modules)
        configs = discover_configs(base_path)
        modules = parse_module_configs(configs, base_path, os_filter)
        available_modules = set(modules.keys())

        missing = [m for m in resolved_profile_modules if m not in available_modules]
        if missing:
            missing.sort()
            print(json.dumps({
                "error": "invalid_profile",
                "profile": profile_name,
                "missing_modules": missing
            }, indent=2), file=sys.stderr)
            return 1

        # Use profile to filter modules
        module_filter = resolved_profile_modules

    configs = discover_configs(base_path)

    # Validate all configs
    all_valid = True
    validation_results = []
    dock_configs_found = []

    for file_path, content in configs:
        errors = validate_config(content, file_path)
        valid = len(errors) == 0
        if not valid:
            all_valid = False

        file_result = {
            "path": str(file_path.relative_to(base_path)),
            "valid": valid
        }
        if not valid:
            file_result["errors"] = errors

        validation_results.append(file_result)

    if len(dock_configs_found) > 1:
        all_valid = False
        dock_files = [str(f.relative_to(base_path)) for f in dock_configs_found]
        for dock_file in dock_configs_found:
            validation_results.append({
                "path": str(dock_file.relative_to(base_path)),
                "valid": False,
                "errors": [f"multiple 'dock' schemas found - only one is allowed (found in: {', '.join(dock_files)})"]
            })

    if os_version_filter and os_version_filter not in OS_VERSIONS:
        print(json.dumps({
            "error": "unknown_os_version",
            "details": f"unknown version '{os_version_filter}'"
        }, indent=2), file=sys.stderr)
        return 1

    if not all_valid:
        validation_results.sort(key=lambda x: x["path"])
        print(json.dumps({
            "error": "validation_failed",
            "details": {"valid": False, "files": validation_results}
        }, indent=2), file=sys.stderr)
        return 1

    # Parse configs into ModuleConfig objects
    modules = parse_module_configs(configs, base_path, os_filter)

    # Filter by module name if specified
    if module_filter:
        modules = {k: v for k, v in modules.items() if k in module_filter}

    # Validate dependencies
    missing = find_missing_dependencies(modules)
    if missing:
        module_name = next(iter(missing))
        print(json.dumps({
            "error": "missing_dependency",
            "module": module_name,
            "missing": missing[module_name]
        }), file=sys.stderr)
        return 1

    # Check for cycles
    cycle = detect_cycle(modules)
    if cycle:
        print(json.dumps({
            "error": "circular_dependency",
            "cycle": cycle
        }), file=sys.stderr)
        return 1

    # Topological sort
    sorted_modules = topological_sort(modules)

    # Generate module plans
    plan_modules = []
    conflict_data: dict = {}

    for module in sorted_modules:
        plan = generate_module_plan(module, os_filter, os_version_filter)
        plan_modules.append(plan)

        conflict_data[module.name] = {
            "file_actions": [
                a for a in plan["actions"]
                if a["type"] in ("link", "copy")
            ]
        }

    # Check for conflicts
    conflict_json = detect_conflicts(conflict_data)
    if conflict_json:
        print(json.dumps(conflict_json, indent=2), file=sys.stderr)
        return 1

    output = {"modules": plan_modules}
    print(json.dumps(output, indent=2))
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Generate JSON execution plan for system preferences, dock layout, and environments."
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # Validate command
    validate_parser = subparsers.add_parser("validate", help="Validate rig configs")
    validate_parser.add_argument("dir", help="Directory to validate")

    # Plan command
    plan_parser = subparsers.add_parser("plan", help="Generate provisioning plan")
    plan_parser.add_argument("dir", help="Directory to plan")
    plan_parser.add_argument("--os", dest="os_filter", choices=["macos", "linux"], help="Filter by OS name")
    plan_parser.add_argument("--os-version", dest="os_version_filter", help="Filter by OS version (affects preferences only)")
    plan_parser.add_argument("--module", dest="module_filter", action="append", help="Filter modules by name (can be specified multiple times)")
    plan_parser.add_argument("--profile", dest="profile_name", help="Use a profile from the manifest")

    # List-profiles command
    list_profiles_parser = subparsers.add_parser("list-profiles", help="List resolved profiles")
    list_profiles_parser.add_argument("dir", help="Directory to inspect")

    args = parser.parse_args()

    if args.command == "validate":
        exit_code = validate_command(args.dir)
        sys.exit(exit_code)
    elif args.command == "plan":
        exit_code = plan_command(
            args.dir,
            args.os_filter,
            args.os_version_filter,
            args.module_filter,
            args.profile_name
        )
        sys.exit(exit_code)
    elif args.command == "list-profiles":
        exit_code = list_profiles_command(args.dir)
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
