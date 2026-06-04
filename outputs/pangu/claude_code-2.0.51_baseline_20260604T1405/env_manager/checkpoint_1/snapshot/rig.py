#!/usr/bin/env python3
"""System Provisioning Planner - Dry-run CLI for rig configuration management."""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

import yaml


def discover_configs(root_dir: Path) -> list[tuple[Path, Optional[dict], Optional[str]]]:
    """Discover all rig config files in directory.

    Returns list of (file_path, parsed_content, parse_error_if_any).
    Only includes files that have a 'version' key at top level.
    """
    configs = []

    for file_path in root_dir.rglob("*"):
        # Skip .git directories
        if ".git" in file_path.parts:
            continue

        # Check extension
        if file_path.suffix.lower() not in (".yaml", ".yml", ".json"):
            continue

        content, parse_error = parse_config(file_path)

        if parse_error:
            configs.append((file_path, None, parse_error))
            continue

        # Must have 'version' key
        if not isinstance(content, dict) or "version" not in content:
            continue

        configs.append((file_path, content, None))

    # Sort by file path
    configs.sort(key=lambda x: x[0])
    return configs


def parse_config(file_path: Path) -> tuple[Optional[dict], Optional[str]]:
    """Parse config file, return (content, parse_error_if_any)."""
    try:
        with open(file_path, "r") as f:
            content = yaml.safe_load(f)
    except yaml.YAMLError as e:
        return None, f"parse error: {e}"
    except Exception as e:
        return None, f"parse error: {e}"

    return content, None


def validate_config(file_path: Path, content: dict) -> list[str]:
    """Validate a config content against schema.

    Returns list of error messages. Empty list means valid.
    """
    errors = []
    rel_path = file_path.name  # just the filename for error messages

    # 1. version: must be a string
    if "version" not in content:
        errors.append(f"'{rel_path}': version required")
        return errors  # Can't continue without version

    version = content["version"]
    if not isinstance(version, str):
        errors.append(f"'{rel_path}': version must be a string")
        return errors

    if version != "1":
        errors.append(f"'{rel_path}': unsupported version '{version}'")
        return errors

    # 2. schema required, type, and recognized
    if "schema" not in content:
        errors.append(f"'{rel_path}': schema required")
    else:
        schema = content["schema"]
        if not isinstance(schema, str):
            errors.append(f"'{rel_path}': schema must be a string")
        elif schema != "module":
            errors.append(f"'{rel_path}': unsupported schema '{schema}'")

    # 3. os type
    if "os" in content:
        os_val = content["os"]
        if not isinstance(os_val, dict):
            errors.append(f"'{rel_path}': os must be an object")
            # Skip further os validation
        else:
            # 4. os.name
            if "name" not in os_val:
                errors.append(f"'{rel_path}': os.name required")
            else:
                os_name = os_val["name"]
                if not isinstance(os_name, str):
                    errors.append(f"'{rel_path}': os.name must be a string")
                elif os_name not in ("macos", "linux"):
                    errors.append(f"'{rel_path}': os.name must be 'macos' or 'linux'")

            # 5. os.package_manager
            if "package_manager" in os_val:
                pm = os_val["package_manager"]
                if not isinstance(pm, str):
                    errors.append(f"'{rel_path}': os.package_manager must be a string")
                elif pm not in ("brew", "apt", "yum"):
                    errors.append(f"'{rel_path}': os.package_manager must be 'brew', 'apt', or 'yum'")

            # Check if packages/apps present, then package_manager required
            has_packages = "packages" in os_val
            has_applications = "applications" in os_val
            if (has_packages or has_applications) and "package_manager" not in os_val:
                errors.append(f"'{rel_path}': os.package_manager required when os.packages or os.applications present")

            # 6. os.packages
            if "packages" in os_val:
                packages = os_val["packages"]
                if not isinstance(packages, list):
                    errors.append(f"'{rel_path}': os.packages must be a list")
                else:
                    for i, pkg in enumerate(packages):
                        if not isinstance(pkg, str):
                            errors.append(f"'{rel_path}': os.packages[{i}] must be a string")
                        elif not pkg:
                            errors.append(f"'{rel_path}': os.packages[{i}] must be a non-empty string")

            # 7. os.applications
            if "applications" in os_val:
                apps = os_val["applications"]
                if not isinstance(apps, list):
                    errors.append(f"'{rel_path}': os.applications must be a list")
                else:
                    for i, app in enumerate(apps):
                        if not isinstance(app, str):
                            errors.append(f"'{rel_path}': os.applications[{i}] must be a string")
                        elif not app:
                            errors.append(f"'{rel_path}': os.applications[{i}] must be a non-empty string")

    # 8. actions type and item-object checks
    if "actions" in content:
        actions = content["actions"]
        if not isinstance(actions, list):
            errors.append(f"'{rel_path}': actions must be a list")
        else:
            for i, action in enumerate(actions):
                if not isinstance(action, dict):
                    errors.append(f"'{rel_path}': actions[{i}] must be an object")
                    continue

                # 10. actions[N].type required/type/known checks
                if "type" not in action:
                    errors.append(f"'{rel_path}': actions[{i}].type required")
                    action_type = None
                else:
                    action_type = action["type"]
                    if not isinstance(action_type, str):
                        errors.append(f"'{rel_path}': actions[{i}].type must be a string")
                    elif action_type not in ("link", "copy", "run"):
                        errors.append(f"'{rel_path}': actions[{i}].type must be 'link', 'copy', or 'run'")

                # 11. actions[N].source required/type/emptiness
                if "source" not in action:
                    errors.append(f"'{rel_path}': actions[{i}].source required")
                    source = None
                else:
                    source = action["source"]
                    if not isinstance(source, str):
                        errors.append(f"'{rel_path}': actions[{i}].source must be a string")
                    elif not source:
                        errors.append(f"'{rel_path}': actions[{i}].source must be a non-empty string")

                # Wildcard source validation
                if source == "*":
                    # 12. destination required for wildcard, must end with /
                    if "destination" not in action:
                        errors.append(f"'{rel_path}': actions[{i}].destination required when using wildcard source")
                    elif not isinstance(action["destination"], str):
                        errors.append(f"'{rel_path}': actions[{i}].destination must be a string when using wildcard source")
                    elif not action["destination"].endswith("/"):
                        errors.append(f"'{rel_path}': actions[{i}].destination must end with '/' when using wildcard source")
                elif source is not None:
                    # Non-wildcard source: must not start with /
                    if source.startswith("/"):
                        errors.append(f"'{rel_path}': actions[{i}].source must not start with '/' for non-wildcard source")

                # 12. actions[N].destination for link/copy
                if action_type in ("link", "copy"):
                    if "destination" not in action:
                        errors.append(f"'{rel_path}': actions[{i}].destination required for '{action_type}' type")
                    elif not isinstance(action["destination"], str):
                        errors.append(f"'{rel_path}': actions[{i}].destination must be a string")
                    elif not action["destination"]:
                        errors.append(f"'{rel_path}': actions[{i}].destination must be a non-empty string")
                    elif action["destination"] and action["destination"] != "*":
                        # Check if starts with ~ or /
                        dest = action["destination"]
                        if not (dest.startswith("~") or dest.startswith("/")):
                            errors.append(f"'{rel_path}': actions[{i}].destination must start with '/' or '~'")

                # 13. actions[N].hidden and elevated type checks
                if "hidden" in action:
                    if not isinstance(action["hidden"], bool):
                        errors.append(f"'{rel_path}': actions[{i}].hidden must be a boolean")

                if "elevated" in action:
                    if not isinstance(action["elevated"], bool):
                        errors.append(f"'{rel_path}': actions[{i}].elevated must be a boolean")

    return errors


def validate_cmd(root_dir: Path):
    """Validate all config files in directory."""
    configs = discover_configs(root_dir)

    files_result = []
    all_valid = True

    for file_path, content, parse_error in configs:
        rel_path = str(file_path.relative_to(root_dir))

        if parse_error:
            files_result.append({
                "path": rel_path,
                "valid": False,
                "errors": [parse_error]
            })
            all_valid = False
            continue

        # Must be a module config (has schema: module)
        if content is None or content.get("schema") != "module":
            # Not a module config, skip
            continue

        errors = validate_config(file_path, content)

        if errors:
            files_result.append({
                "path": rel_path,
                "valid": False,
                "errors": errors
            })
            all_valid = False
        else:
            files_result.append({
                "path": rel_path,
                "valid": True
            })

    # Sort files by path
    files_result.sort(key=lambda x: x["path"])

    result = {
        "valid": all_valid,
        "files": files_result
    }

    output = json.dumps(result, indent=2)
    print(output)

    return 0 if all_valid else 1


def get_module_configs(root_dir: Path) -> list[tuple[dict, Path]]:
    """Return list of (content, module_dir) for all valid module configs."""
    configs = discover_configs(root_dir)
    modules = []

    for file_path, content, parse_error in configs:
        if parse_error:
            continue
        # Must have schema: module
        if content is None or content.get("schema") != "module":
            continue

        module_dir = file_path.parent
        modules.append((content, module_dir))

    # Check for duplicate module schemas in same directory
    dir_modules = {}
    for content, dir_path in modules:
        if dir_path not in dir_modules:
            dir_modules[dir_path] = []
        dir_modules[dir_path].append(content)

    # Report errors for directories with multiple module configs
    # This is validation step 14
    for dir_path, module_list in dir_modules.items():
        if len(module_list) > 1:
            # This is a validation error
            rel_dir = str(dir_path.relative_to(root_dir))
            # Format error JSON to match validate output
            error_json = {
                "error": "validation_failed",
                "details": {
                    "valid": False,
                    "files": [{
                        "path": rel_dir,
                        "valid": False,
                        "errors": [f"duplicate 'module' schema in '{rel_dir}'"]
                    }]
                }
            }
            print(json.dumps(error_json, indent=2), file=sys.stderr)
            sys.exit(1)

    return modules


def filter_modules(modules: list[tuple[dict, Path]], os_filter: Optional[str], module_filter: Optional[list[str]]) -> list[tuple[dict, Path]]:
    """Filter modules by OS and/or module name."""
    filtered = []

    for content, module_dir in modules:
        module_name = str(module_dir.relative_to(module_dir.parent))  # Just the directory name

        # Check OS filter
        if os_filter:
            module_os = content.get("os", {}).get("name")
            if module_os is not None and module_os != os_filter:
                continue

        # Check module name filter
        if module_filter:
            if module_name not in module_filter:
                continue

        filtered.append((content, module_dir))

    return filtered


def resolve_destination(destination: str, home: Path) -> str:
    """Expand ~ in destination to absolute path."""
    if destination.startswith("~"):
        # Remove ~ and prepend home
        rest = destination[1:]
        # Handle ~/something -> /home/user/something
        if rest:
            if not rest.startswith(("/", "\\")):
                rest = "/" + rest
        else:
            rest = "/"
        result = str(home / rest.lstrip("/"))
        return result
    return destination


def generate_plan_cmd(root_dir: Path, os_filter: Optional[str], module_filter: Optional[list[str]]):
    """Generate provisioning plan."""
    try:
        modules = get_module_configs(root_dir)
    except SystemExit:
        return 1

    # First, validate all configs
    # Report validation errors if any
    validation_errors = []
    for file_path, content, parse_error in discover_configs(root_dir):
        if parse_error:
            validation_errors.append({
                "path": str(file_path.relative_to(root_dir)),
                "valid": False,
                "errors": [parse_error]
            })
            continue

        # Must be module config
        if content is None or content.get("schema") != "module":
            continue

        errors = validate_config(file_path, content)
        if errors:
            validation_errors.append({
                "path": str(file_path.relative_to(root_dir)),
                "valid": False,
                "errors": errors
            })

    if validation_errors:
        error_result = {
            "error": "validation_failed",
            "details": {
                "valid": False,
                "files": validation_errors
            }
        }
        print(json.dumps(error_result, indent=2), file=sys.stderr)
        return 1

    # Apply filters
    filtered_modules = filter_modules(modules, os_filter, module_filter)

    if not filtered_modules:
        # Output empty modules array
        result = {"modules": []}
        print(json.dumps(result, indent=2))
        return 0

    # Sort modules by name (alphabetical)
    filtered_modules.sort(key=lambda x: str(x[1].relative_to(root_dir)))

    plan = {"modules": []}
    home = Path.home()

    for content, module_dir in filtered_modules:
        module_name = module_dir.name  # Directory name
        actions = []

        # Get OS info
        os_config = content.get("os", {})
        package_manager = os_config.get("package_manager", "")

        # Add package installation actions
        for pkg in os_config.get("packages", []):
            actions.append({
                "type": "install_package",
                "manager": package_manager,
                "package": pkg
            })

        # Add application installation actions
        for app in os_config.get("applications", []):
            actions.append({
                "type": "install_application",
                "manager": package_manager,
                "application": app
            })

        # Add file actions
        for action in content.get("actions", []):
            action_type = action["type"]
            source = action["source"]
            hidden = action.get("hidden", False)
            elevated = action.get("elevated", False)

            # Wildcard expansion
            if source == "*":
                # Find all files in module directory (excluding directories, yaml/json, hidden)
                wildcard_files = []
                for item in module_dir.iterdir():
                    if item.is_file() and not item.name.startswith("."):
                        ext = item.suffix.lower()
                        if ext not in (".yaml", ".yml", ".json"):
                            wildcard_files.append(item)

                # Sort by filename
                wildcard_files.sort(key=lambda x: x.name)

                destination_base = action["destination"]

                for file_item in wildcard_files:
                    filename = file_item.name

                    # Apply hidden modifier
                    if hidden and not filename.startswith("."):
                        filename = "." + filename

                    dest = destination_base + filename
                    dest_abs = resolve_destination(dest, home)

                    actions.append({
                        "type": "link",
                        "source": str(file_item.resolve()),
                        "destination": dest_abs,
                        "elevated": elevated
                    })

            else:
                # Non-wildcard source
                source_path = module_dir / source

                # Check file existence
                if not source_path.exists():
                    # file_not_found error
                    error_detail = json.dumps({
                        "error": "file_not_found",
                        "details": {
                            "module": module_name,
                            "file": source
                        }
                    })
                    print(error_detail, file=sys.stderr)
                    return 1

                # Prepare action
                abs_source = str(source_path.resolve())

                if action_type == "link":
                    dest = action["destination"]
                    if hidden:
                        # Modify destination filename to be hidden
                        dest_dir = os.path.dirname(dest)
                        dest_file = os.path.basename(dest)
                        if dest_dir == "":
                            # No directory, just the filename
                            if not dest_file.startswith("."):
                                dest_file = "." + dest_file
                            dest = dest_file
                        else:
                            if not dest_file.startswith("."):
                                dest_file = "." + dest_file
                            dest = os.path.join(dest_dir, dest_file)

                    dest_abs = resolve_destination(dest, home)

                    actions.append({
                        "type": "link",
                        "source": abs_source,
                        "destination": dest_abs,
                        "elevated": elevated
                    })

                elif action_type == "copy":
                    dest = action["destination"]
                    if hidden:
                        dest_dir = os.path.dirname(dest)
                        dest_file = os.path.basename(dest)
                        if dest_dir == "":
                            if not dest_file.startswith("."):
                                dest_file = "." + dest_file
                            dest = dest_file
                        else:
                            if not dest_file.startswith("."):
                                dest_file = "." + dest_file
                            dest = os.path.join(dest_dir, dest_file)

                    dest_abs = resolve_destination(dest, home)

                    actions.append({
                        "type": "copy",
                        "source": abs_source,
                        "destination": dest_abs,
                        "elevated": elevated
                    })

                elif action_type == "run":
                    # Run actions never emit destination even if present
                    actions.append({
                        "type": "run",
                        "source": abs_source,
                        "elevated": elevated
                    })

        # Sort actions within module
        # Packages sorted by name
        package_actions = sorted(
            [a for a in actions if a["type"] == "install_package"],
            key=lambda x: x["package"]
        )
        # Applications sorted by name
        app_actions = sorted(
            [a for a in actions if a["type"] == "install_application"],
            key=lambda x: x["application"]
        )
        # File actions in original order
        file_actions = [a for a in actions if a["type"] in ("link", "copy", "run")]

        sorted_actions = package_actions + app_actions + file_actions

        plan["modules"].append({
            "name": module_name,
            "actions": sorted_actions
        })

    output = json.dumps(plan, indent=2)
    print(output)
    return 0


def main():
    parser = argparse.ArgumentParser(description="Rig system provisioning planner")
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Validate command
    validate_parser = subparsers.add_parser("validate", help="Validate rig configs")
    validate_parser.add_argument("dir", help="Directory containing rig configs")

    # Plan command
    plan_parser = subparsers.add_parser("plan", help="Generate provisioning plan")
    plan_parser.add_argument("dir", help="Directory containing rig configs")
    plan_parser.add_argument("--os", choices=["macos", "linux"],
                           help="Filter by operating system")
    plan_parser.add_argument("--module", action="append", dest="modules",
                           help="Filter by module name (can be used multiple times)")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return 1

    root_dir = Path(args.dir).resolve()

    if args.command == "validate":
        return validate_cmd(root_dir)
    elif args.command == "plan":
        return generate_plan_cmd(root_dir, args.os, args.modules)

    return 1


if __name__ == "__main__":
    sys.exit(main())
