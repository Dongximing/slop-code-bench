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
    parser.add_argument("command", choices=["plan"], help="Command to run")
    parser.add_argument("dir", type=Path, help="Directory containing configuration files")
    parser.add_argument("--os", help="Filter by operating system")
    parser.add_argument("--os-version", help="Filter by OS version")
    parser.add_argument("--module", action="append", dest="modules", help="Filter by module name(s)")
    return parser.parse_args()


def load_yaml_file(path: Path) -> dict:
    """Load and parse a YAML file."""
    with open(path, "r") as f:
        return yaml.safe_load(f)


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


def discover_configs(base_dir: Path, os_filter: str = None, module_filter: list = None) -> dict:
    """Discover and load all configuration files from directory tree."""
    configs = {
        "modules": {},  # module_name -> {configs, file_paths}
        "dock_configs": [],  # list of file paths with dock schema
    }

    # Find all YAML files
    yaml_files = list(base_dir.rglob("*.yaml")) + list(base_dir.rglob("*.yml"))

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
            }

        module_info = configs["modules"][module_name]
        module_info["configs"].append(data)
        module_info["file_paths"].append(yaml_file)

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


def generate_plan(configs: dict, os_filter: str = None, os_version: str = None) -> dict:
    """Generate the execution plan from validated configs."""
    modules_plan = []

    for module_name, module_info in configs["modules"].items():
        actions = []

        # Collect actions from different schemas
        package_actions = []
        application_actions = []
        file_actions = []
        preference_actions = []
        dock_action = None

        # Track if module should be included (based on OS filter)
        module_included = False
        module_os_match = True  # Default to True if no os filter

        for config in module_info["configs"]:
            schema = config.get("schema")

            if schema == "module":
                # Handle module config
                os_config = config.get("os", {})
                os_name = os_config.get("name")

                # Check OS filter
                if os_filter:
                    if os_name != os_filter:
                        module_os_match = False
                        continue
                    module_os_match = True

                module_included = True

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

        # If module OS doesn't match filter and we have no other configs, skip
        if os_filter and not module_os_match:
            continue

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

        # Build final action list in order
        actions = package_actions + application_actions + file_actions + preference_actions

        # 5. configure_dock (comes last)
        if dock_action:
            actions.append(dock_action)

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

        # Check for conflicts (after module filtering)
        conflicts = check_conflicts(configs, args.os, args.os_version, args.dir)
        if conflicts:
            print(json.dumps({"error": "conflict", "conflicts": conflicts}), file=sys.stderr)
            sys.exit(1)

        # Generate plan
        plan = generate_plan(configs, args.os, args.os_version)

        # Output plan as JSON
        print(json.dumps(plan, indent=2))

    except (ValidationError, ValueError) as e:
        # Handle errors
        if isinstance(e, ValidationError):
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
            # Handle unknown_os_version error from ValueError
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
