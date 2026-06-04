#!/usr/bin/env python3
"""System Preferences, Dock Layout, and Conflict Detection

Generates JSON execution plans, describing system preferences, Dock layout, and destination conflicts.
"""

import argparse
import json
import os
import sys
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


# ============================================================================
# Error Handling
# ============================================================================

class ValidationError(Exception):
    """Raised when configuration validation fails."""
    pass


# ============================================================================
# Config Discovery
# ============================================================================

def discover_config_files(root_dir: Path) -> list[tuple[Path, dict]]:
    """Discover all YAML config files in a directory tree.

    Returns a list of (file_path, parsed_content) tuples.
    Files that can't be parsed are excluded.
    """
    configs = []
    for yaml_file in sorted(root_dir.rglob("*.yaml")):
        try:
            content = yaml.safe_load(yaml_file.read_text())
            if content and "schema" in content:
                configs.append((yaml_file, content))
        except Exception:
            # Skip unparsable files
            pass
    return configs


# ============================================================================
# Validation
# ============================================================================

def validate_preferences_config(content: dict, file_path: Path) -> list[str]:
    """Validate a preferences schema configuration.

    Returns a list of error messages (empty if valid).
    """
    errors = []

    # preferences must be a list
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

        # name is required
        if "name" not in pref or not isinstance(pref["name"], str) or not pref["name"]:
            errors.append(f"preferences[{i}].name: required string")

        # domain is required
        if "domain" not in pref or not isinstance(pref["domain"], str) or not pref["domain"]:
            errors.append(f"preferences[{i}].domain: required string")

        # key is required
        if "key" not in pref or not isinstance(pref["key"], str) or not pref["key"]:
            errors.append(f"preferences[{i}].key: required string")

        # value is required
        if "value" not in pref:
            errors.append(f"preferences[{i}].value: required")

        # value_type is required and valid
        if "value_type" not in pref:
            errors.append(f"preferences[{i}].value_type: required")
        elif not isinstance(pref["value_type"], str):
            errors.append(f"preferences[{i}].value_type: must be a string")
        elif pref["value_type"] not in VALID_VALUE_TYPES:
            errors.append(
                f"preferences[{i}].value_type: must be 'bool', 'int', 'string', or 'float', got '{pref['value_type']}'"
            )

        # apply_command is required
        if "apply_command" not in pref or not isinstance(pref["apply_command"], str) or not pref["apply_command"]:
            errors.append(f"preferences[{i}].apply_command: required string")

        # check_command optional, string if present
        if "check_command" in pref and not isinstance(pref["check_command"], str):
            errors.append(f"preferences[{i}].check_command: must be a string")

        # expected_state optional, string if present
        if "expected_state" in pref and not isinstance(pref["expected_state"], str):
            errors.append(f"preferences[{i}].expected_state: must be a string")

        # enabled optional, boolean if present
        if "enabled" in pref and not isinstance(pref["enabled"], bool):
            errors.append(f"preferences[{i}].enabled: must be a boolean")

        # min_version optional, must be valid
        if "min_version" in pref:
            if not isinstance(pref["min_version"], str):
                errors.append(f"preferences[{i}].min_version: must be a string")
            elif pref["min_version"] not in OS_VERSIONS:
                errors.append(f"preferences[{i}].min_version: unknown version '{pref['min_version']}'")

        # max_version optional, must be valid
        if "max_version" in pref:
            if not isinstance(pref["max_version"], str):
                errors.append(f"preferences[{i}].max_version: must be a string")
            elif pref["max_version"] not in OS_VERSIONS:
                errors.append(f"preferences[{i}].max_version: unknown version '{pref['max_version']}'")

        # Check version range
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
    """Validate a dock schema configuration.

    Returns a list of error messages (empty if valid).
    """
    errors = []

    # os is required and must be macos
    if "os" not in content or content["os"] != "macos":
        errors.append("dock: 'os' is required and must be 'macos'")

    # items is required and must be non-empty list of strings
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
    """Validate a module schema configuration.

    Returns a list of error messages (empty if valid).
    """
    errors = []

    # os check
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

            # package_manager check
            if "package_manager" in os_val:
                pm = os_val["package_manager"]
                if not isinstance(pm, str):
                    errors.append("os.package_manager: must be a string")
                elif pm not in ("brew", "apt", "yum"):
                    errors.append('os.package_manager: must be "brew", "apt", or "yum"')

            # package_manager required when packages or applications present
            has_packages = "packages" in os_val
            has_apps = "applications" in os_val
            if has_packages or has_apps:
                if "package_manager" not in os_val:
                    errors.append("os.package_manager: required when packages or applications present")
                elif isinstance(os_val.get("package_manager"), str) and os_val["package_manager"] not in ("brew", "apt", "yum"):
                    # Error already reported above
                    pass

            # packages check
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

            # applications check
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

    # actions check (for module schema)
    if "actions" in content:
        actions = content["actions"]
        if not isinstance(actions, list):
            errors.append("actions: must be a list")
        else:
            for i, action in enumerate(actions):
                if not isinstance(action, dict):
                    errors.append(f"actions[{i}]: must be an object")
                    continue

                # type check
                if "type" not in action:
                    errors.append(f"actions[{i}].type: missing")
                elif not isinstance(action["type"], str):
                    errors.append(f"actions[{i}].type: must be a string")
                elif action["type"] not in ("link", "copy", "run"):
                    errors.append(f"actions[{i}].type: must be 'link', 'copy', or 'run'")

                # source check
                if "source" not in action:
                    errors.append(f"actions[{i}].source: missing")
                elif not isinstance(action["source"], str):
                    errors.append(f"actions[{i}].source: must be a string")
                elif not action["source"]:
                    errors.append(f"actions[{i}].source: must be non-empty")
                elif action["source"].startswith("/"):
                    errors.append(f"actions[{i}].source: must not start with /")

                # destination check
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

                # hidden check
                if "hidden" in action and not isinstance(action["hidden"], bool):
                    errors.append(f"actions[{i}].hidden: must be a boolean")

                # elevated check
                if "elevated" in action and not isinstance(action["elevated"], bool):
                    errors.append(f"actions[{i}].elevated: must be a boolean")

    return errors


def validate_config(content: dict, file_path: Path) -> list[str]:
    """Validate a configuration file.

    Returns a list of error messages (empty if valid).
    """
    errors = []

    # version check
    if "version" not in content:
        errors.append("version: missing")
    else:
        version = content["version"]
        if not isinstance(version, str):
            errors.append("version: must be a string")
        elif version != "1":
            errors.append(f"unsupported version '{version}'")

    # schema check
    if "schema" not in content:
        if "version" in content and isinstance(content["version"], str) and content["version"] == "1":
            errors.append("schema: missing")
    else:
        schema = content["schema"]
        if not isinstance(schema, str):
            errors.append("schema: must be a string")
        elif schema not in ("module", "preferences", "dock"):
            errors.append(f"schema: unrecognized schema '{schema}'")

    # Skip further validation if version is invalid
    if "version" in content:
        if not isinstance(content["version"], str):
            return errors
        if content["version"] != "1":
            return errors

    # Validate based on schema
    schema = content.get("schema", "")

    if schema == "preferences":
        errors.extend(validate_preferences_config(content, file_path))
    elif schema == "dock":
        errors.extend(validate_dock_config(content, file_path))
        dock_configs_found.append(file_path)
    elif schema == "module":
        errors.extend(validate_module_config(content, file_path))

    return errors


# ============================================================================
# OS Version Helpers
# ============================================================================

def is_version_in_range(
    pref_min: Optional[str],
    pref_max: Optional[str],
    target_version: Optional[str]
) -> bool:
    """Check if target_version is within the preference's version range.

    If target_version is None, returns True (include all).
    If min_version is None, starts from earliest version.
    If max_version is None, ends at latest version.
    """
    if target_version is None:
        return True

    min_idx = 0 if pref_min is None else OS_VERSIONS.index(pref_min)
    max_idx = len(OS_VERSIONS) - 1 if pref_max is None else OS_VERSIONS.index(pref_max)
    target_idx = OS_VERSIONS.index(target_version)

    return min_idx <= target_idx <= max_idx


# ============================================================================
# Conflict Detection
# ============================================================================

def detect_conflicts(modules_data: dict) -> Optional[dict]:
    """Detect conflicts where multiple file actions write to the same destination.

    Returns conflict JSON if conflicts found, None otherwise.
    """
    # Map destination -> list of sources
    dest_map: dict[str, list[dict]] = {}

    for module_name, module_data in modules_data.items():
        for action in module_data.get("file_actions", []):
            if action["type"] in ("link", "copy"):
                dest = action["destination"]
                if dest not in dest_map:
                    dest_map[dest] = []
                dest_map[dest].append({
                    "module": module_name,
                    "type": action["type"],
                    "source": action["source"]
                })

    # Find conflicts (destinations with >1 source)
    conflicts = []
    for dest in sorted(dest_map.keys()):
        sources = dest_map[dest]
        if len(sources) > 1:
            # Sort sources by module, then source, then type
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
# Config Parsing
# ============================================================================

def parse_configs(configs: list[tuple[Path, dict]], base_dir: Path) -> dict:
    """Parse and organize all configs by module.

    Returns a dict: {module_name: {schema_type: config_data, ...}}
    """
    modules: dict[str, dict] = {}

    for file_path, content in configs:
        module_name = file_path.parent.name
        schema = content.get("schema")

        if module_name not in modules:
            modules[module_name] = {}

        # Store the config under its schema type
        if schema == "module":
            # Module configs can be named module.yaml in module directory
            # or they might be in a file named after the module
            modules[module_name]["module"] = content
        elif schema == "preferences":
            if "preferences" in modules[module_name]:
                # Merge preferences lists
                modules[module_name]["preferences"]["preferences"].extend(content["preferences"])
            else:
                modules[module_name]["preferences"] = content
        elif schema == "dock":
            modules[module_name]["dock"] = content

    return modules


# ============================================================================
# Action Generation
# ============================================================================

def generate_file_actions(module_config: dict, module_dir: Path) -> list[dict]:
    """Generate file actions (link, copy, run) from module config."""
    actions = []

    for action in module_config.get("actions", []):
        act_type = action["type"]
        source = action["source"]
        elevated = action.get("elevated", False)

        if source == "*":
            # Wildcard expansion - match non-hidden, non-config files
            for item in sorted(module_dir.iterdir()):
                if item.is_dir():
                    continue
                if item.name.startswith("."):
                    continue
                if item.suffix.lower() in (".yaml", ".yml", ".json"):
                    continue

                filename = item.name
                dest = action["destination"] + filename

                # Apply hidden modifier
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

                # Resolve ~
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
            src_path = module_dir / source
            src_path_str = str(src_path) if not src_path.is_absolute() else str(src_path)

            if act_type == "run":
                actions.append({
                    "type": "run",
                    "source": src_path_str,
                    "elevated": elevated
                })
            else:
                dest = action["destination"]

                # Apply hidden modifier
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

                # Resolve ~
                if dest.startswith("~"):
                    dest = str(Path.home()) + dest[1:]

                actions.append({
                    "type": act_type,
                    "source": src_path_str,
                    "destination": dest,
                    "elevated": elevated
                })

    return actions


def generate_preference_actions(prefs_config: dict, os_version_filter: Optional[str]) -> list[dict]:
    """Generate set_preference actions from preferences config."""
    actions = []

    for pref in prefs_config.get("preferences", []):
        # Skip if not enabled
        if not pref.get("enabled", True):
            continue

        # Apply OS version filter
        if not is_version_in_range(
            pref.get("min_version"),
            pref.get("max_version"),
            os_version_filter
        ):
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

        # Add optional fields only if present
        if "check_command" in pref:
            action["check_command"] = pref["check_command"]
        if "expected_state" in pref:
            action["expected_state"] = pref["expected_state"]

        actions.append(action)

    return actions


def generate_dock_action(dock_config: dict, os_filter: Optional[str]) -> Optional[dict]:
    """Generate configure_dock action if OS filter matches."""
    if os_filter is not None and os_filter != "macos":
        return None

    return {
        "type": "configure_dock",
        "items": dock_config["items"]
    }


def generate_module_plan(
    module_name: str,
    module_data: dict,
    module_dir: Path,
    os_filter: Optional[str],
    os_version_filter: Optional[str],
    module_filter: Optional[list[str]]
) -> Optional[dict]:
    """Generate the complete action plan for a module."""

    # Check module filter
    if module_filter and module_name not in module_filter:
        return None

    module_config = module_data.get("module", {})

    # Apply OS filter at module level
    if os_filter:
        module_os = module_config.get("os", {}).get("name")
        if module_os and module_os != os_filter:
            return None

    # Generate all actions
    all_actions = []

    # File actions (link, copy, run) - from module config
    if module_config.get("actions"):
        file_actions = generate_file_actions(module_config, module_dir)
        all_actions.extend(file_actions)

    # Package actions (install_package) - from module config
    packages = module_config.get("os", {}).get("packages", [])
    pm = module_config.get("os", {}).get("package_manager", "")
    for pkg in packages:
        all_actions.append({
            "type": "install_package",
            "manager": pm,
            "package": pkg
        })

    # Application actions (install_application) - from module config
    applications = module_config.get("os", {}).get("applications", [])
    for app in applications:
        all_actions.append({
            "type": "install_application",
            "manager": pm,
            "application": app
        })

    # Preference actions (set_preference) - from preferences config
    if "preferences" in module_data:
        pref_actions = generate_preference_actions(
            module_data["preferences"],
            os_version_filter
        )
        all_actions.extend(pref_actions)

    # Dock action (configure_dock) - from dock config
    if "dock" in module_data:
        dock_action = generate_dock_action(module_data["dock"], os_filter)
        if dock_action:
            all_actions.append(dock_action)

    # Sort actions according to ordering rules
    all_actions = sort_actions(all_actions)

    return {
        "name": module_name,
        "actions": all_actions
    }


def sort_actions(actions: list[dict]) -> list[dict]:
    """Sort actions according to ordering rules.

    Order:
    1. install_package - sorted alphabetically by package
    2. install_application - sorted alphabetically by application
    3. file actions (link, copy, run) - in config order
    4. set_preference - in order they appear in preferences list
    5. configure_dock - last
    """
    # Group by type
    pkg_actions = [a for a in actions if a["type"] == "install_package"]
    app_actions = [a for a in actions if a["type"] == "install_application"]
    file_actions = [a for a in actions if a["type"] in ("link", "copy", "run")]
    pref_actions = [a for a in actions if a["type"] == "set_preference"]
    dock_actions = [a for a in actions if a["type"] == "configure_dock"]

    # Sort package actions alphabetically by package
    pkg_actions.sort(key=lambda a: a["package"])

    # Sort application actions alphabetically by application
    app_actions.sort(key=lambda a: a["application"])

    # File actions stay in config order (already in correct order)
    # Preference actions stay in original order
    # Dock action already last

    return pkg_actions + app_actions + file_actions + pref_actions + dock_actions


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

    configs = discover_config_files(base_path)
    results = []
    all_valid = True

    # Reset dock configs found
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

    # Check for multiple dock configs (global validation)
    dock_errors = check_multiple_dock_configs()
    if dock_errors:
        all_valid = False
        for dock_file in dock_configs_found:
            results.append({
                "path": str(dock_file.relative_to(base_path)),
                "valid": False,
                "errors": dock_errors
            })

    # Sort by path for consistent output
    results.sort(key=lambda x: x["path"])

    output = {
        "valid": all_valid,
        "files": results
    }
    print(json.dumps(output, indent=2))
    return 0 if all_valid else 1


def check_multiple_dock_configs() -> list[str]:
    """Check for multiple dock configs using the global dock_configs_found list."""
    global dock_configs_found
    dock_files = [str(f) for f in dock_configs_found]

    if len(dock_files) > 1:
        return [f"multiple 'dock' schemas found - only one is allowed (found in: {', '.join(dock_files)})"]
    return []


def plan_command(base_dir: str, os_filter: Optional[str], os_version_filter: Optional[str], module_filter: Optional[list[str]]) -> int:
    """Implement the 'plan' command."""
    global dock_configs_found

    base_path = Path(base_dir)
    if not base_path.exists():
        print(json.dumps({"error": "directory not found"}), file=sys.stderr)
        return 1

    # Discover configs
    configs = discover_config_files(base_path)

    # First pass: validate all configs
    all_valid = True
    validation_results = []
    global dock_configs_found
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

    # Check for multiple dock configs (global validation)
    dock_errors = check_multiple_dock_configs(configs)
    if dock_errors:
        all_valid = False
        for dock_file in dock_configs_found:
            validation_results.append({
                "path": str(dock_file.relative_to(base_path)),
                "valid": False,
                "errors": dock_errors
            })

    # Validate OS version filter
    if os_version_filter and os_version_filter not in OS_VERSIONS:
        print(json.dumps({
            "error": "unknown_os_version",
            "details": f"unknown version '{os_version_filter}'"
        }, indent=2), file=sys.stderr)
        return 1

    if not all_valid:
        # Sort by path for consistent output
        validation_results.sort(key=lambda x: x["path"])
        print(json.dumps({
            "error": "validation_failed",
            "details": {"valid": False, "files": validation_results}
        }, indent=2), file=sys.stderr)
        return 1

    # Parse configs into modules
    modules_data = parse_configs(configs, base_path)

    # Generate module plans
    modules = []
    conflict_data: dict = {}

    for module_name in sorted(modules_data.keys()):
        module_dir = base_path / module_name
        plan = generate_module_plan(
            module_name,
            modules_data[module_name],
            module_dir,
            os_filter,
            os_version_filter,
            module_filter
        )

        if plan:
            modules.append(plan)
            # Collect file actions for conflict detection
            conflict_data[module_name] = {
                "file_actions": [
                    a for a in plan["actions"]
                    if a["type"] in ("link", "copy")
                ]
            }

    # Check for conflicts after all filtering
    conflict_json = detect_conflicts(conflict_data)
    if conflict_json:
        print(json.dumps(conflict_json, indent=2), file=sys.stderr)
        return 1

    # Output plan
    output = {"modules": modules}
    print(json.dumps(output, indent=2))
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Generate JSON execution plan for system preferences and dock layout."
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

    args = parser.parse_args()

    if args.command == "validate":
        exit_code = validate_command(args.dir)
        sys.exit(exit_code)
    elif args.command == "plan":
        exit_code = plan_command(
            args.dir,
            args.os_filter,
            args.os_version_filter,
            args.module_filter
        )
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
