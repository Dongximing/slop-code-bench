#!/usr/bin/env python3
"""
System Preferences, Dock Layout, and Conflict Detection

Generate JSON execution plan describing system preferences, Dock layout, and destination conflicts.
"""

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import yaml


class SchemaType(Enum):
    MODULE = "module"
    PREFERENCES = "preferences"
    DOCK = "dock"
    ENVIRONMENTS = "environments"
    MANIFEST = "manifest"


# OS version ordering (oldest to newest)
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

VALID_VALUE_TYPES = {"bool", "int", "string", "float"}


@dataclass
class ValidationError:
    """Represents a validation error."""
    message: str
    file: Optional[str] = None
    line: Optional[int] = None

    def to_dict(self) -> dict:
        result = {"error": "validation_error"}
        result["message"] = self.message
        if self.file:
            result["file"] = self.file
        return result


def error_response(error_type: str, details: Optional[dict] = None) -> str:
    """Create a JSON error response for plan/list-profiles errors."""
    result = {"error": error_type}
    if details:
        result.update(details)
    return json.dumps(result)


@dataclass
class PreferenceEntry:
    """Represents a single preference entry."""
    name: str
    domain: str
    key: str
    value: Any
    value_type: str
    apply_command: str
    check_command: Optional[str] = None
    expected_state: Optional[str] = None
    min_version: Optional[str] = None
    max_version: Optional[str] = None
    enabled: bool = True

    def to_plan_action(self) -> dict:
        action = {
            "type": "set_preference",
            "name": self.name,
            "domain": self.domain,
            "key": self.key,
            "value": self.value,
            "value_type": self.value_type,
            "apply_command": self.apply_command,
        }
        if self.check_command is not None:
            action["check_command"] = self.check_command
        if self.expected_state is not None:
            action["expected_state"] = self.expected_state
        return action

    def matches_version(self, os_version: Optional[str]) -> bool:
        """Check if this preference should be included for the given OS version."""
        if not self.enabled:
            return False
        if os_version is None:
            return True

        if os_version not in OS_VERSIONS:
            return False

        min_idx = 0
        if self.min_version is not None:
            min_idx = OS_VERSIONS.index(self.min_version)

        max_idx = len(OS_VERSIONS) - 1
        if self.max_version is not None:
            max_idx = OS_VERSIONS.index(self.max_version)

        target_idx = OS_VERSIONS.index(os_version)

        return min_idx <= target_idx <= max_idx


@dataclass
class DockEntry:
    """Represents a dock configuration."""
    os: str
    items: list[str]

    def to_plan_action(self) -> dict:
        return {
            "type": "configure_dock",
            "items": self.items,
        }


@dataclass
class FileAction:
    """Represents a file action from module.yaml."""
    type: str
    source: str
    destination: Optional[str] = None
    hidden: bool = False
    elevated: bool = False


@dataclass
class EnvironmentVersion:
    """Represents a runtime environment version."""
    version: str
    name: str


@dataclass
class EnvironmentConfig:
    """Represents an environment configuration from environments.yaml."""
    language: str
    versions: list[str]
    manager: str
    plugins: list[dict[str, Any]] = field(default_factory=list)  # Each plugin has 'name' and optional 'virtual_environments'


@dataclass
class ManifestProfile:
    """Represents a single profile definition in a manifest."""
    name: str
    modules: list[str]
    extends: Optional[str] = None


@dataclass
class ModuleConfig:
    """Represents a complete module configuration."""
    name: str
    directory: Path
    schema_type: SchemaType
    depends_on: list[str] = field(default_factory=list)
    preferences: list[PreferenceEntry] = field(default_factory=list)
    dock: Optional[DockEntry] = None
    packages: list[str] = field(default_factory=list)
    package_manager: Optional[str] = None
    file_actions: list[FileAction] = field(default_factory=list)
    applications: list[str] = field(default_factory=list)
    environments: list[EnvironmentConfig] = field(default_factory=list)


def discover_configs(root_dir: Path) -> tuple[list[ModuleConfig], list[ValidationError], list[str]]:
    """
    Discover all config files in the directory tree.
    Returns (modules, validation_errors, dock_files).
    """
    errors = []
    modules = []
    dock_files = []
    dir_configs: dict[Path, dict[str, Any]] = {}

    # Find all YAML files
    yaml_files = list(root_dir.rglob("*.yaml")) + list(root_dir.rglob("*.yml"))

    # Group by directory
    for yaml_file in yaml_files:
        dir_path = yaml_file.parent
        if dir_path not in dir_configs:
            dir_configs[dir_path] = {}
        dir_configs[dir_path][yaml_file.name] = yaml_file

    # Process each directory
    for dir_path, files in dir_configs.items():
        module_name = dir_path.name
        module_dir = dir_path

        # Look for specific schema files or any yaml file with schema field
        module_file = files.get("module.yaml") or files.get("module.yml")
        prefs_file = files.get("preferences.yaml") or files.get("preferences.yml")
        dock_file = files.get("dock.yaml") or files.get("dock.yml")

        # If we don't have a specific schema file, look for any yaml file with a schema field
        if not (module_file or prefs_file or dock_file):
            for fname, fpath in files.items():
                data, parse_errors = parse_yaml_file(fpath)
                errors.extend(parse_errors)
                if data and data.get("schema") == "module":
                    module_file = fpath
                    break
                elif data and data.get("schema") == "preferences":
                    prefs_file = fpath
                    break
                elif data and data.get("schema") == "dock":
                    dock_file = fpath
                    break

        # Parse all configs for this directory
        module_schema = None
        preferences_data = None
        dock_data = None

        # Parse module.yaml if exists
        if module_file:
            data, parse_errors = parse_yaml_file(module_file)
            errors.extend(parse_errors)
            if data:
                if data.get("schema") == "module":
                    module_schema = SchemaType.MODULE
                elif data.get("schema") not in (None, "module"):
                    errors.append(ValidationError(
                        message=f"Unknown schema '{data.get('schema')}'",
                        file=str(module_file.relative_to(root_dir))
                    ))

        # Parse preferences.yaml if exists
        if prefs_file:
            data, parse_errors = parse_yaml_file(prefs_file)
            errors.extend(parse_errors)
            if data and data.get("schema") == "preferences":
                preferences_data = data
            elif data and data.get("schema") not in (None, "preferences"):
                errors.append(ValidationError(
                    message=f"Unknown schema '{data.get('schema')}'",
                    file=str(prefs_file.relative_to(root_dir))
                ))

        # Parse dock.yaml if exists
        if dock_file:
            data, parse_errors = parse_yaml_file(dock_file)
            errors.extend(parse_errors)
            if data and data.get("schema") == "dock":
                dock_data = data
                dock_files.append(str(dock_file.relative_to(root_dir)))
            elif data and data.get("schema") not in (None, "dock"):
                errors.append(ValidationError(
                    message=f"Unknown schema '{data.get('schema')}'",
                    file=str(dock_file.relative_to(root_dir))
                ))

        # Parse environments.yaml if exists
        env_file = files.get("environments.yaml") or files.get("environments.yml")
        if env_file:
            data, parse_errors = parse_yaml_file(env_file)
            errors.extend(parse_errors)
            if data and data.get("schema") == "environments":
                environments_data = data
            elif data and data.get("schema") not in (None, "environments"):
                errors.append(ValidationError(
                    message=f"Unknown schema '{data.get('schema')}'",
                    file=str(env_file.relative_to(root_dir))
                ))

        # Create module config if we have a schema or config
        if module_schema or preferences_data or dock_data or environments_data:
            module = ModuleConfig(
                name=module_name,
                directory=module_dir,
                schema_type=module_schema if module_schema else SchemaType.PREFERENCES if preferences_data else SchemaType.ENVIRONMENTS if environments_data else SchemaType.DOCK
            )

            # Parse preferences if present
            if preferences_data:
                prefs_list = preferences_data.get("preferences", [])
                for i, pref in enumerate(prefs_list):
                    pref_errors = validate_preference_entry(pref, i, prefs_file.name if prefs_file else None)
                    errors.extend(pref_errors)

                    if not pref_errors:
                        entry = PreferenceEntry(
                            name=pref.get("name", ""),
                            domain=pref.get("domain", ""),
                            key=pref.get("key", ""),
                            value=pref.get("value"),
                            value_type=pref.get("value_type", ""),
                            apply_command=pref.get("apply_command", ""),
                            check_command=pref.get("check_command"),
                            expected_state=pref.get("expected_state"),
                            min_version=pref.get("min_version"),
                            max_version=pref.get("max_version"),
                            enabled=pref.get("enabled", True),
                        )
                        module.preferences.append(entry)

            # Parse dock if present
            if dock_data:
                dock_errors = validate_dock_entry(dock_data, dock_file.name if dock_file else None)
                errors.extend(dock_errors)

                if not dock_errors:
                    module.dock = DockEntry(
                        os=dock_data.get("os", ""),
                        items=dock_data.get("items", []),
                    )

            # Parse module.yaml for packages, apps, file actions
            if module_file:
                data, parse_errors = parse_yaml_file(module_file)
                errors.extend(parse_errors)
                if data and data.get("schema") == "module":
                    os_config = data.get("os", {})
                    if isinstance(os_config, dict):
                        module.package_manager = os_config.get("package_manager")
                        module.packages = os_config.get("packages", [])
                        module.applications = os_config.get("applications", [])

                    actions = data.get("actions", [])
                    if isinstance(actions, list):
                        for action_data in actions:
                            action_errors = validate_file_action(action_data)
                            errors.extend(action_errors)
                            if not action_errors:
                                action = FileAction(
                                    type=action_data.get("type", ""),
                                    source=action_data.get("source", ""),
                                    destination=action_data.get("destination"),
                                    hidden=action_data.get("hidden", False),
                                    elevated=action_data.get("elevated", False),
                                )
                                module.file_actions.append(action)

            modules.append(module)

    # Check for multiple dock configs
    if len(dock_files) > 1:
        errors.append(ValidationError(
            message="multiple 'dock' schemas found — only one is allowed",
            file=dock_files[0]
        ))

    return modules, errors, dock_files


def discover_manifest(root_dir: Path) -> tuple[Optional[dict], list[ValidationError]]:
    """
    Discover the manifest file in the root directory.
    Returns (manifest_data, errors).
    """
    errors = []

    # Look for manifest files in the root directory
    manifest_candidates = []
    for yaml_file in root_dir.glob("*.yaml"):
        data, parse_errors = parse_yaml_file(yaml_file)
        errors.extend(parse_errors)
        if data and data.get("schema") == "manifest":
            manifest_candidates.append((yaml_file, data))

    # Check for multiple manifests
    if len(manifest_candidates) > 1:
        errors.append(ValidationError(
            message="multiple 'manifest' schemas found — only one is allowed",
            file=str(manifest_candidates[0][0].relative_to(root_dir))
        ))
        return None, errors

    if len(manifest_candidates) == 0:
        return None, errors

    return manifest_candidates[0][1], errors


def validate_manifest(manifest_data: dict, root_dir: Path, all_module_names: set[str] = None) -> list[ValidationError]:
    """
    Validate the manifest structure and content.
    """
    errors = []

    # Check profiles exists and is an object
    if "profiles" not in manifest_data:
        errors.append(ValidationError(
            message="profiles is required and must be a non-empty object",
            file="manifest.yaml"
        ))
        return errors

    profiles = manifest_data["profiles"]
    if not isinstance(profiles, dict):
        errors.append(ValidationError(
            message="profiles must be a non-empty object",
            file="manifest.yaml"
        ))
        return errors

    if len(profiles) == 0:
        errors.append(ValidationError(
            message="profiles must be a non-empty object",
            file="manifest.yaml"
        ))
        return errors

    # Check each profile
    for profile_name, profile_def in profiles.items():
        if not isinstance(profile_def, dict):
            errors.append(ValidationError(
                message=f"profiles.{profile_name} must be an object",
                file="manifest.yaml"
            ))
            continue

        # Check modules exists and is non-empty list
        if "modules" not in profile_def:
            errors.append(ValidationError(
                message=f"profiles.{profile_name}.modules is required",
                file="manifest.yaml"
            ))
            continue

        modules = profile_def["modules"]
        if not isinstance(modules, list):
            errors.append(ValidationError(
                message=f"profiles.{profile_name}.modules must be a list",
                file="manifest.yaml"
            ))
            continue

        if len(modules) == 0:
            errors.append(ValidationError(
                message=f"profiles.{profile_name}.modules must be a non-empty list",
                file="manifest.yaml"
            ))
            continue

        # Check each module entry is a non-empty string
        for i, module in enumerate(modules):
            if not isinstance(module, str):
                errors.append(ValidationError(
                    message=f"profiles.{profile_name}.modules[{i}] must be a string",
                    file="manifest.yaml"
                ))
            elif module == "":
                errors.append(ValidationError(
                    message=f"profiles.{profile_name}.modules[{i}] must be a non-empty string",
                    file="manifest.yaml"
                ))

        # Check extends exists in profiles
        if "extends" in profile_def:
            extends_name = profile_def["extends"]
            if extends_name not in profiles:
                errors.append(ValidationError(
                    message=f"profiles.{profile_name}.extends: unknown profile '{extends_name}'",
                    file="manifest.yaml"
                ))

    # Check manifest is in root directory (not module directory)
    # We'll check this separately when discovering
    return errors


def resolve_profile_inheritance(manifest_data: dict) -> dict[str, list[str]]:
    """
    Resolve profile inheritance and return resolved module sets.
    Raises PlanError on circular inheritance.
    """
    profiles = manifest_data.get("profiles", {})

    # Detect circular inheritance using DFS
    def detect_cycle():
        visited = {}  # 0 = unvisited, 1 = visiting, 2 = visited

        def visit(name, path):
            if name in visited:
                if visited[name] == 1:  # Currently visiting - cycle detected
                    # Find the cycle
                    start_idx = path.index(name)
                    cycle = path[start_idx:] + [name]
                    return cycle
                return None
            visited[name] = 1  # Mark as visiting
            path.append(name)

            profile_def = profiles.get(name)
            if profile_def and "extends" in profile_def:
                parent = profile_def["extends"]
                if parent in profiles:
                    cycle = visit(parent, path)
                    if cycle:
                        return cycle

            path.pop()
            visited[name] = 2  # Mark as visited
            return None

        for name in profiles:
            if name not in visited:
                cycle = visit(name, [])
                if cycle:
                    return cycle
        return None

    cycle = detect_cycle()
    if cycle:
        raise PlanError("circular_inheritance", {"cycle": cycle})

    # Resolve profiles using memoization
    resolved = {}

    def resolve_profile(name):
        if name in resolved:
            return resolved[name]

        profile_def = profiles.get(name)
        if not profile_def:
            return []

        # Start with empty set, then inherit from parent
        modules_set = set()

        # Inherit from parent
        if "extends" in profile_def:
            parent_name = profile_def["extends"]
            parent_modules = resolve_profile(parent_name)
            modules_set.update(parent_modules)

        # Add own modules (deduplicated)
        own_modules = profile_def.get("modules", [])
        for module in own_modules:
            modules_set.add(module)

        resolved[name] = list(modules_set)
        return list(modules_set)

    # Resolve all profiles
    result = {}
    for name in profiles:
        result[name] = resolve_profile(name)

    return result


def get_resolved_modules_for_plan(modules: list[ModuleConfig], manifest_data: Optional[dict],
                                  profile_name: Optional[str], module_filter: Optional[list[str]]) -> tuple[list[ModuleConfig], list[ValidationError]]:
    """
    Get modules that should be included in the plan, considering profile and module filters.
    Returns (filtered_modules, errors).
    """
    errors = []

    # Get all module names from discovered modules
    all_module_names = {m.name for m in modules}

    # If no manifest or no profile, use all modules
    if not manifest_data:
        if profile_name:
            raise PlanError("no_manifest", {"details": "no manifest found but --profile was specified"})
        # No profile specified, use module filter if any
        if module_filter:
            filtered = [m for m in modules if m.name in module_filter]
            return filtered, errors
        return modules, errors

    # Validate manifest
    manifest_errors = validate_manifest(manifest_data, Path("."), all_module_names)
    if manifest_errors:
        return [], manifest_errors

    # Resolve inheritance
    resolved_profiles = resolve_profile_inheritance(manifest_data)

    if not profile_name:
        # No profile specified, use module filter if any
        if module_filter:
            filtered = [m for m in modules if m.name in module_filter]
            return filtered, errors
        return modules, errors

    # Check profile exists
    if profile_name not in resolved_profiles:
        raise PlanError("unknown_profile", {"details": f"unknown profile '{profile_name}'"})

    # Get resolved modules for the profile
    profile_modules = resolved_profiles[profile_name]

    # Check for missing modules
    missing = [m for m in profile_modules if m not in all_module_names]
    if missing:
        missing.sort()
        raise PlanError("invalid_profile", {"profile": profile_name, "missing_modules": missing})

    # Apply module filter if present (intersection)
    if module_filter:
        # Filter to only include modules that are both in the resolved profile AND in the module filter
        filtered_names = set(profile_modules) & set(module_filter)
        # Also include dependencies of filtered modules
        module_map = {m.name: m for m in modules}
        filtered_modules = list(filtered_names)  # Start with direct matches
        to_check = list(filtered_names)
        while to_check:
            name = to_check.pop()
            if name in module_map:
                for dep in module_map[name].depends_on:
                    if dep in all_module_names and dep not in filtered_modules:
                        filtered_modules.append(dep)
                        to_check.append(dep)
        return [module_map[name] for name in filtered_modules if name in module_map], errors

    # Return all resolved profile modules
    module_map = {m.name: m for m in modules}
    result_modules = [module_map[name] for name in profile_modules if name in module_map]
    return result_modules, errors


def generate_plan(
    modules: list[ModuleConfig],
    os_filter: Optional[str] = None,
    os_version: Optional[str] = None,
    module_filter: Optional[list[str]] = None,
    profile_name: Optional[str] = None,
    manifest_data: Optional[dict] = None,
) -> tuple[list[dict], list[dict]]:
    """
    Generate the execution plan with filtering and conflict detection.
    Returns (plan_modules, conflicts).
    """

    # Apply filters using get_resolved_modules_for_plan
    filtered_modules, filter_errors = get_resolved_modules_for_plan(
        modules, manifest_data, profile_name, module_filter
    )
    if filter_errors:
        return [], []

    # OS filter for dock (only allow macos)
    for module in filtered_modules:
        if module.dock and os_filter != "macos":
            module.dock = None

    # Generate actions for each module
    plan_modules = []
    file_actions_for_conflict = []  # For conflict detection

    for module in filtered_modules:
        actions = []

        # Add package actions (sorted alphabetically)
        for pkg in sorted(module.packages):
            action = {
                "type": "install_package",
                "manager": module.package_manager or "unknown",
                "package": pkg,
            }
            actions.append(action)

        # Add application actions (sorted alphabetically)
        for app in sorted(module.applications):
            action = {
                "type": "install_application",
                "application": app,
            }
            actions.append(action)

        # Add file actions in config order
        for fa in module.file_actions:
            if fa.source == "*":
                # Wildcard expansion
                for entry in sorted(module.directory.iterdir()):
                    if entry.is_dir():
                        continue
                    if entry.suffix.lower() in (".yaml", ".yml", ".json"):
                        continue
                    if entry.name.startswith("."):
                        continue
                    dest = fa.destination + entry.name if fa.destination else ""
                    if fa.hidden and not entry.name.startswith("."):
                        # Hidden file handling
                        base_dir = os.path.dirname(fa.destination) if fa.destination else ""
                        dest = base_dir + "/." + entry.name
                    final_dest = resolve_home(dest) if dest else ""
                    actions.append({
                        "type": "link",
                        "source": str(entry),
                        "destination": final_dest,
                        "elevated": fa.elevated,
                    })
            else:
                source_abs = resolve_source_path(fa.source, module.directory)
                dest = None
                if fa.destination:
                    dest = resolve_home(fa.destination)
                actions.append({
                    "type": fa.type,
                    "source": source_abs,
                    "destination": dest,
                    "elevated": fa.elevated,
                })

        # Add preference actions (in order, filtered by version)
        for pref in module.preferences:
            if pref.matches_version(os_version):
                actions.append(pref.to_plan_action())

        # Add dock action (last, if exists)
        if module.dock:
            actions.append(module.dock.to_plan_action())

        plan_modules.append({"name": module.name, "actions": actions})

        # Collect file actions for conflict detection
        for action in actions:
            if action["type"] in ("link", "copy") and "destination" in action:
                file_actions_for_conflict.append({
                    "module": module.name,
                    "type": action["type"],
                    "source": action["source"],
                    "destination": action["destination"],
                })

    # Detect conflicts
    conflicts = detect_conflicts(file_actions_for_conflict)

    return plan_modules, conflicts


def parse_yaml_file(file_path: Path) -> tuple[Optional[dict], list[ValidationError]]:
    errors = []
    try:
        with open(file_path, "r") as f:
            data = yaml.safe_load(f)
            if data is None:
                errors.append(ValidationError(
                    message=f"empty file",
                    file=str(file_path.relative_to(file_path.parent.parent))
                ))
                return None, errors
            return data, errors
    except yaml.YAMLError as e:
        errors.append(ValidationError(
            message=f"YAML parse error: {e}",
            file=str(file_path.relative_to(file_path.parent.parent))
        ))
        return None, errors
    except Exception as e:
        errors.append(ValidationError(
            message=f"error reading file: {e}",
            file=str(file_path.relative_to(file_path.parent.parent))
        ))
        return None, errors


def validate_preference_entry(data: dict, index: int, file_name: Optional[str]) -> list[ValidationError]:
    errors = []
    prefix = f"preferences[{index}]"
    file_ref = file_name if file_name else None

    # Required fields
    for field in ["name", "domain", "key", "value", "value_type", "apply_command"]:
        if field not in data:
            errors.append(ValidationError(
                message=f"{prefix}.{field} is required",
                file=file_ref
            ))

    # Validate value_type
    if "value_type" in data:
        vt = data["value_type"]
        if vt not in VALID_VALUE_TYPES:
            errors.append(ValidationError(
                message=f"{prefix}.value_type: must be 'bool', 'int', 'string', or 'float', got '{vt}'",
                file=file_ref
            ))

    # Validate min_version
    if "min_version" in data:
        mv = data["min_version"]
        if mv not in OS_VERSIONS:
            errors.append(ValidationError(
                message=f"{prefix}.min_version: unknown version '{mv}'",
                file=file_ref
            ))

    # Validate max_version
    if "max_version" in data:
        mv = data["max_version"]
        if mv not in OS_VERSIONS:
            errors.append(ValidationError(
                message=f"{prefix}.max_version: unknown version '{mv}'",
                file=file_ref
            ))

    # Validate version range
    if "min_version" in data and "max_version" in data:
        min_v = data["min_version"]
        max_v = data["max_version"]
        if min_v in OS_VERSIONS and max_v in OS_VERSIONS:
            if OS_VERSIONS.index(min_v) > OS_VERSIONS.index(max_v):
                errors.append(ValidationError(
                    message=f"{prefix}: min_version '{min_v}' is later than max_version '{max_v}'",
                    file=file_ref
                ))

    return errors


def validate_dock_entry(data: dict, file_name: Optional[str]) -> list[ValidationError]:
    """Validate a dock entry."""
    errors = []
    file_ref = file_name if file_name else None

    # os field
    if "os" not in data or data["os"] != "macos":
        errors.append(ValidationError(
            message="dock: 'os' is required and must be 'macos'",
            file=file_ref
        ))

    # items field
    if "items" not in data:
        errors.append(ValidationError(
            message="dock: 'items' is required",
            file=file_ref
        ))
    elif not isinstance(data["items"], list) or len(data["items"]) == 0:
        errors.append(ValidationError(
            message="dock: 'items' must be a non-empty list",
            file=file_ref
        ))
    elif not all(isinstance(item, str) for item in data["items"]):
        errors.append(ValidationError(
            message="dock: 'items' must be a list of strings",
            file=file_ref
        ))

    return errors


def validate_file_action(data: dict) -> list[ValidationError]:
    """Validate a single file action from module.yaml."""
    errors = []

    # type
    if "type" not in data:
        errors.append(ValidationError(message="action missing 'type'"))
    elif data["type"] not in ("link", "copy", "run"):
        errors.append(ValidationError(
            message=f"action.type must be 'link', 'copy', or 'run', got '{data['type']}'"
        ))

    # source
    if "source" not in data:
        errors.append(ValidationError(message="action missing 'source'"))
    elif not isinstance(data["source"], str):
        errors.append(ValidationError(message="action.source must be a string"))
    elif data["source"] == "":
        errors.append(ValidationError(message="action.source must not be empty"))

    # destination for link/copy
    if data.get("type") in ("link", "copy"):
        if "destination" not in data:
            errors.append(ValidationError(
                message=f"action missing 'destination' for type '{data['type']}'"
            ))
        elif not isinstance(data["destination"], str):
            errors.append(ValidationError(message="action.destination must be a string"))
        elif not data["destination"].startswith(("/", "~")):
            errors.append(ValidationError(
                message="action.destination must start with '/' or '~'"
            ))
        elif data["source"] == "*" and not data["destination"].endswith("/"):
            errors.append(ValidationError(
                message="action.destination must end with '/' for wildcard source"
            ))

    # hidden, elevated type checks
    for field in ("hidden", "elevated"):
        if field in data and not isinstance(data[field], bool):
            errors.append(ValidationError(
                message=f"action.{field} must be a boolean"
            ))

    return errors




def resolve_home(path: str) -> str:
    """Replace ~ with $HOME."""
    if path.startswith("~"):
        home = os.environ.get("HOME", "")
        if path == "~":
            return home
        return home + path[1:]
    return path


def resolve_source_path(source: str, module_dir: Path) -> str:
    """Resolve a source path to an absolute path."""
    if source.startswith("/"):
        return source
    return str(module_dir / source)



def detect_conflicts(file_actions: list[dict]) -> list[dict]:
    """Detect conflicts where multiple actions target the same destination."""
    dest_map: dict[str, list[dict]] = {}

    for action in file_actions:
        dest = action["destination"]
        if dest not in dest_map:
            dest_map[dest] = []
        dest_map[dest].append(action)

    conflicts = []
    for dest, sources in sorted(dest_map.items()):
        if len(sources) > 1:
            # Sort sources by module, then source, then type
            sorted_sources = sorted(sources, key=lambda s: (s["module"], s["source"], s["type"]))
            conflict_sources = [
                {"module": s["module"], "type": s["type"], "source": s["source"]}
                for s in sorted_sources
            ]
            conflicts.append({
                "destination": dest,
                "sources": conflict_sources,
            })

    return conflicts


# --- CLI commands

def cmd_validate(args):
    """Implement validate command."""
    base_dir = Path(args.dir)

    if not base_dir.exists():
        print(json.dumps({"valid": False, "errors": [f"Directory not found: {base_dir}"]}))
        return 1

    modules, errors, _ = discover_configs(base_dir)

    file_errors = []
    for err in errors:
        file_errors.append({"error": err.message, "file": err.file})

    all_valid = len(errors) == 0
    result = {"valid": all_valid, "errors": file_errors}
    print(json.dumps(result, indent=2))
    return 0 if all_valid else 1


def cmd_plan(args):
    """Implement plan command."""
    base_dir = Path(args.dir)

    if not base_dir.exists():
        print(json.dumps({"error": f"Directory not found: {base_dir}"}), file=sys.stderr)
        return 1

    # Discover and validate configs
    modules, errors, dock_files = discover_configs(base_dir)

    # If any validation errors, report and exit
    if errors:
        error_list = [err.to_dict() for err in errors]
        print(json.dumps(error_list), file=sys.stderr)
        return 1

    # Discover manifest
    manifest_data, manifest_errors = discover_manifest(base_dir)
    errors.extend(manifest_errors)

    # If any errors from manifest discovery, report and exit
    if errors:
        error_list = [err.to_dict() for err in errors]
        print(json.dumps(error_list), file=sys.stderr)
        return 1

    # Generate plan
    try:
        plan_modules, conflicts = generate_plan(
            modules,
            os_filter=args.os,
            os_version=args.os_version,
            module_filter=args.module,
            profile_name=args.profile,
            manifest_data=manifest_data,
        )
    except PlanError as e:
        print(error_response(e.error_type, e.details), file=sys.stderr)
        return 1

    # Check for conflicts
    if conflicts:
        conflict_json = {
            "error": "conflict",
            "conflicts": conflicts
        }
        print(json.dumps(conflict_json), file=sys.stderr)
        return 1

    # Output successful plan
    plan = {"modules": plan_modules}
    print(json.dumps(plan, indent=2))
    return 0


def cmd_list_profiles(args):
    """Implement list-profiles command."""
    base_dir = Path(args.dir)

    if not base_dir.exists():
        print(json.dumps({"error": f"Directory not found: {base_dir}"}), file=sys.stderr)
        return 1

    # Discover and validate configs (for module name lookups)
    modules, errors, dock_files = discover_configs(base_dir)

    # If any validation errors, report and exit
    if errors:
        error_list = [err.to_dict() for err in errors]
        print(json.dumps(error_list), file=sys.stderr)
        return 1

    # Discover manifest
    manifest_data, manifest_errors = discover_manifest(base_dir)

    # If no manifest, return empty profiles list
    if not manifest_data:
        print(json.dumps({"profiles": []}))
        return 0

    # Get all module names from discovered modules
    all_module_names = {m.name for m in modules}

    # Validate manifest
    manifest_errors = validate_manifest(manifest_data, Path("."), all_module_names)
    if manifest_errors:
        error_list = [err.to_dict() for err in manifest_errors]
        print(json.dumps(error_list), file=sys.stderr)
        return 1

    # Resolve inheritance
    try:
        resolved_profiles = resolve_profile_inheritance(manifest_data)
    except PlanError as e:
        print(error_response(e.error_type, e.details), file=sys.stderr)
        return 1

    # Check for missing modules and sort results alphabetically
    profiles_list = []
    for profile_name in sorted(resolved_profiles.keys()):
        profile_modules = resolved_profiles[profile_name]

        # Check for missing modules
        missing = [m for m in profile_modules if m not in all_module_names]
        if missing:
            missing.sort()
            print(error_response("invalid_profile", {"profile": profile_name, "missing_modules": missing}), file=sys.stderr)
            return 1

        # Sort modules alphabetically
        sorted_modules = sorted(profile_modules)
        profiles_list.append({"name": profile_name, "modules": sorted_modules})

    result = {"profiles": profiles_list}
    print(json.dumps(result, indent=2))
    return 0


# --- Main

def main():
    parser = argparse.ArgumentParser(prog="rig.py")
    subparsers = parser.add_subparsers(dest="command")

    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("dir", help="Directory to validate")

    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("dir", help="Directory to plan")
    plan_parser.add_argument("--os", choices=["macos", "linux"], help="Filter by OS name")
    plan_parser.add_argument("--os-version", choices=OS_VERSIONS, help="Filter by OS version (affects preferences only)")
    plan_parser.add_argument("--module", nargs="*", help="Filter by module names")
    plan_parser.add_argument("--profile", help="Use a named profile from the manifest")

    list_profiles_parser = subparsers.add_parser("list-profiles")
    list_profiles_parser.add_argument("dir", help="Directory to list profiles")

    args = parser.parse_args()

    if args.command == "validate":
        ret = cmd_validate(args)
        sys.exit(ret)
    elif args.command == "plan":
        ret = cmd_plan(args)
        sys.exit(ret)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
