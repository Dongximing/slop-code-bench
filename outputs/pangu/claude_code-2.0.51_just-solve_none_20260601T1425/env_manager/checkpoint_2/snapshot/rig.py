#!/usr/bin/env python3
"""System Preferences, Dock Layout, and Conflict Detection tool."""

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


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
    message: str
    field: Optional[str] = None


@dataclass
class Preference:
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


@dataclass
class DockConfig:
    os: str
    items: list[str]


@dataclass
class ModuleConfig:
    name: str
    preferences: list[Preference] = field(default_factory=list)
    dock: Optional[DockConfig] = None
    packages: list[str] = field(default_factory=list)
    package_manager: Optional[str] = None
    applications: list[str] = field(default_factory=list)
    links: list[dict] = field(default_factory=list)
    copies: list[dict] = field(default_factory=list)
    runs: list[dict] = field(default_factory=list)


def parse_yaml_file(filepath: Path) -> Optional[dict]:
    """Parse a YAML file and return its contents."""
    try:
        with open(filepath, "r") as f:
            return yaml.safe_load(f)
    except yaml.YAMLError as e:
        print(f"Error parsing {filepath}: {e}", file=sys.stderr)
        return None


def validate_preference(pref_data: dict, index: int) -> list[ValidationError]:
    """Validate a preference entry."""
    errors = []

    # Check value_type
    value_type = pref_data.get("value_type")
    if value_type not in VALID_VALUE_TYPES:
        errors.append(ValidationError(
            message=f"preferences[{index}].value_type: must be 'bool', 'int', 'string', or 'float', got '{value_type}'",
            field=f"preferences[{index}].value_type"
        ))

    # Check min_version
    min_version = pref_data.get("min_version")
    if min_version and min_version not in OS_VERSIONS:
        errors.append(ValidationError(
            message=f"preferences[{index}].min_version: unknown version '{min_version}'",
            field=f"preferences[{index}].min_version"
        ))

    # Check max_version
    max_version = pref_data.get("max_version")
    if max_version and max_version not in OS_VERSIONS:
        errors.append(ValidationError(
            message=f"preferences[{index}].max_version: unknown version '{max_version}'",
            field=f"preferences[{index}].max_version"
        ))

    # Check min_version <= max_version
    if min_version and max_version:
        min_idx = OS_VERSIONS.index(min_version)
        max_idx = OS_VERSIONS.index(max_version)
        if min_idx > max_idx:
            errors.append(ValidationError(
                message=f"preferences[{index}]: min_version '{min_version}' is later than max_version '{max_version}'",
                field=f"preferences[{index}]"
            ))

    return errors


def validate_dock(dock_data: dict, filepath: Path) -> list[ValidationError]:
    """Validate a dock configuration."""
    errors = []

    os_val = dock_data.get("os")
    if os_val != "macos":
        errors.append(ValidationError(
            message="dock: 'os' is required and must be 'macos'",
            field="dock.os"
        ))

    items = dock_data.get("items")
    if not items or not isinstance(items, list) or len(items) == 0:
        errors.append(ValidationError(
            message="dock: items must be a non-empty list of strings",
            field="dock.items"
        ))
    elif not all(isinstance(item, str) for item in items):
        errors.append(ValidationError(
            message="dock: items must be a non-empty list of strings",
            field="dock.items"
        ))

    return errors


def discover_configs(root_dir: Path) -> tuple[list[ModuleConfig], list[ValidationError]]:
    """Discover and parse all config files in the directory tree."""
    modules = {}
    all_errors = []
    dock_files = []

    for yaml_file in root_dir.rglob("*.yaml"):
        rel_path = yaml_file.relative_to(root_dir)
        dir_name = rel_path.parent.name if rel_path.parent.name != "." else ""
        module_name = dir_name if dir_name else yaml_file.stem

        data = parse_yaml_file(yaml_file)
        if data is None:
            continue

        schema = data.get("schema")

        if schema is None:
            continue

        if schema == "module":
            if module_name not in modules:
                modules[module_name] = ModuleConfig(name=module_name)

            module_data = modules[module_name]

            if "os" in data:
                os_info = data["os"]
                if isinstance(os_info, dict):
                    module_data.package_manager = os_info.get("package_manager")
                    module_data.packages = os_info.get("packages", [])
                    module_data.applications = os_info.get("applications", [])

            if "actions" in data:
                for action in data["actions"]:
                    action_type = action.get("type")
                    if action_type == "link":
                        module_data.links.append({
                            "source": action["source"],
                            "destination": action["destination"],
                        })
                    elif action_type == "copy":
                        module_data.copies.append({
                            "source": action["source"],
                            "destination": action["destination"],
                        })
                    elif action_type == "run":
                        module_data.runs.append({
                            "command": action["command"],
                            "description": action.get("description", ""),
                        })

        elif schema == "preferences":
            if module_name not in modules:
                modules[module_name] = ModuleConfig(name=module_name)

            module_data = modules[module_name]

            preferences = data.get("preferences", [])
            for i, pref_data in enumerate(preferences):
                # Validate
                errors = validate_preference(pref_data, i)
                all_errors.extend(errors)

                # Create preference if enabled or if no enabled field (defaults to True)
                if pref_data.get("enabled", True):
                    pref = Preference(
                        name=pref_data["name"],
                        domain=pref_data["domain"],
                        key=pref_data["key"],
                        value=pref_data["value"],
                        value_type=pref_data["value_type"],
                        apply_command=pref_data["apply_command"],
                        check_command=pref_data.get("check_command"),
                        expected_state=pref_data.get("expected_state"),
                        min_version=pref_data.get("min_version"),
                        max_version=pref_data.get("max_version"),
                        enabled=pref_data.get("enabled", True)
                    )
                    module_data.preferences.append(pref)

        elif schema == "dock":
            dock_files.append(yaml_file)

            # Validate dock config
            errors = validate_dock(data, yaml_file)
            all_errors.extend(errors)

            if module_name not in modules:
                modules[module_name] = ModuleConfig(name=module_name)

            modules[module_name].dock = DockConfig(
                os=data["os"],
                items=data["items"]
            )

        else:
            all_errors.append(ValidationError(
                message=f"unrecognized schema '{schema}'",
                field="schema"
            ))

    # Check for multiple dock configs
    if len(dock_files) > 1:
        for filepath in dock_files:
            all_errors.append(ValidationError(
                message="multiple 'dock' schemas found — only one is allowed",
                field="dock"
            ))

    return list(modules.values()), all_errors


def filter_by_os_version(preferences: list[Preference], os_version: Optional[str]) -> list[Preference]:
    """Filter preferences by OS version."""
    if os_version is None:
        return [p for p in preferences if p.enabled]

    if os_version not in OS_VERSIONS:
        raise ValueError(f"unknown os version: {os_version}")

    os_idx = OS_VERSIONS.index(os_version)

    filtered = []
    for p in preferences:
        if not p.enabled:
            continue

        # Check min_version
        if p.min_version:
            min_idx = OS_VERSIONS.index(p.min_version)
            if os_idx < min_idx:
                continue

        # Check max_version
        if p.max_version:
            max_idx = OS_VERSIONS.index(p.max_version)
            if os_idx > max_idx:
                continue

        filtered.append(p)

    return filtered


def create_preference_action(preference: Preference) -> dict:
    """Create a set_preference action from a Preference."""
    action = {
        "type": "set_preference",
        "name": preference.name,
        "domain": preference.domain,
        "key": preference.key,
        "value": preference.value,
        "value_type": preference.value_type,
        "apply_command": preference.apply_command,
    }

    if preference.check_command:
        action["check_command"] = preference.check_command
    if preference.expected_state:
        action["expected_state"] = preference.expected_state

    return action


def create_dock_action(dock: DockConfig) -> dict:
    """Create a configure_dock action from a DockConfig."""
    return {
        "type": "configure_dock",
        "items": dock.items,
    }


def order_actions(module: ModuleConfig) -> list[dict]:
    """Order actions within a module according to the specification."""
    actions = []

    # 1. install_package actions, sorted alphabetically by package
    package_actions = []
    if module.package_manager and module.packages:
        for pkg in sorted(module.packages):
            package_actions.append({
                "type": "install_package",
                "manager": module.package_manager,
                "package": pkg,
            })
    actions.extend(package_actions)

    # 2. install_application actions, sorted alphabetically by application
    app_actions = []
    for app in sorted(module.applications):
        app_actions.append({
            "type": "install_application",
            "application": app,
        })
    actions.extend(app_actions)

    # 3. file actions (link, copy, run) in config order
    for link in module.links:
        actions.append({
            "type": "link",
            "source": link["source"],
            "destination": link["destination"],
        })

    for copy in module.copies:
        actions.append({
            "type": "copy",
            "source": copy["source"],
            "destination": copy["destination"],
        })

    for run in module.runs:
        actions.append({
            "type": "run",
            "command": run["command"],
            "description": run.get("description", ""),
        })

    # 4. set_preference in the order they appear in the preferences list
    for pref in module.preferences:
        actions.append(create_preference_action(pref))

    # 5. configure_dock
    if module.dock:
        actions.append(create_dock_action(module.dock))

    return actions


def detect_conflicts(modules: list[ModuleConfig]) -> list[dict]:
    """Detect conflicts between link and copy actions."""
    dest_to_sources: dict[str, list[dict]] = {}

    for module in modules:
        for link in module.links:
            dest = link["destination"]
            if dest not in dest_to_sources:
                dest_to_sources[dest] = []
            dest_to_sources[dest].append({
                "module": module.name,
                "type": "link",
                "source": link["source"],
            })

        for copy in module.copies:
            dest = copy["destination"]
            if dest not in dest_to_sources:
                dest_to_sources[dest] = []
            dest_to_sources[dest].append({
                "module": module.name,
                "type": "copy",
                "source": copy["source"],
            })

    conflicts = []
    for dest in sorted(dest_to_sources.keys()):
        sources = dest_to_sources[dest]
        if len(sources) > 1:
            # Sort sources by module, then source, then type
            sources.sort(key=lambda s: (s["module"], s["source"], s["type"]))
            conflicts.append({
                "destination": dest,
                "sources": sources,
            })

    return conflicts


def generate_plan(
    modules: list[ModuleConfig],
    os_filter: Optional[str] = None,
    os_version: Optional[str] = None,
    module_filter: Optional[list[str]] = None,
) -> dict:
    """Generate the execution plan."""

    # Filter by OS - dock actions are macOS only
    filtered_modules = []
    for module in modules:
        # Check module filter
        if module_filter and module.name not in module_filter:
            continue

        # Filter preferences by OS version
        filtered_prefs = filter_by_os_version(module.preferences, os_version)

        # Handle OS filter for dock
        dock = module.dock
        if os_filter == "linux" and dock:
            dock = None

        new_module = ModuleConfig(
            name=module.name,
            preferences=filtered_prefs,
            dock=dock,
            packages=module.packages,
            package_manager=module.package_manager,
            applications=module.applications,
            links=module.links,
            copies=module.copies,
            runs=module.runs,
        )
        filtered_modules.append(new_module)

    # Detect conflicts
    conflicts = detect_conflicts(filtered_modules)
    if conflicts:
        error_output = {
            "error": "conflict",
            "conflicts": conflicts,
        }
        print(json.dumps(error_output), file=sys.stderr)
        sys.exit(1)

    # Generate plan
    plan = {"modules": []}
    for module in filtered_modules:
        actions = order_actions(module)
        plan["modules"].append({
            "name": module.name,
            "actions": actions,
        })

    return plan


def main():
    parser = argparse.ArgumentParser(
        description="Generate JSON execution plan for system preferences and Dock layout."
    )
    parser.add_argument("command", help="Command to run (plan)")
    parser.add_argument("directory", help="Directory to scan for config files")
    parser.add_argument("--os", help="Target OS (macos, linux)")
    parser.add_argument("--os-version", help="OS version for preference filtering")
    parser.add_argument("--module", action="append", dest="modules",
                        help="Module name to include (can be specified multiple times)")

    args = parser.parse_args()

    if args.command != "plan":
        print(f"Unknown command: {args.command}", file=sys.stderr)
        sys.exit(1)

    root_dir = Path(args.directory)
    if not root_dir.exists():
        print(f"Directory does not exist: {args.directory}", file=sys.stderr)
        sys.exit(1)

    # Discover configs
    modules, errors = discover_configs(root_dir)

    # Check for unknown OS version early
    if args.os_version and args.os_version not in OS_VERSIONS:
        error_output = {
            "error": "unknown_os_version",
            "details": f"unknown version '{args.os_version}'",
        }
        print(json.dumps(error_output), file=sys.stderr)
        sys.exit(1)

    # Report validation errors
    if errors:
        error_output = {
            "error": "validation_error",
            "errors": [e.message for e in errors],
        }
        print(json.dumps(error_output), file=sys.stderr)
        sys.exit(1)

    # Generate plan
    plan = generate_plan(
        modules,
        os_filter=args.os,
        os_version=args.os_version,
        module_filter=args.modules,
    )

    # Output plan
    print(json.dumps(plan, indent=2))


if __name__ == "__main__":
    main()
