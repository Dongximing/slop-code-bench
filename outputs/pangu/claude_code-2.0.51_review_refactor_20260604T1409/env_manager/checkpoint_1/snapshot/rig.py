#!/usr/bin/env python3
"""System Provisioning Planner MVP - rig.py"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml


# --- Data structures

class ValidationError:
    """Represents a validation error for a single file."""
    def __init__(self, message: str):
        self.message = message

    def to_dict(self) -> dict:
        return {"error": self.message}


class ConfigFile:
    """Represents a discovered config file."""
    def __init__(self, path: str, raw_content: dict, module_dir: str | None = None):
        self.path = path
        self.raw_content = raw_content
        self.module_dir = module_dir  # absolute path of parent directory, or None
        self.errors: list[ValidationError] = []
        self.version = raw_content.get("version")
        self.schema = raw_content.get("schema")
        # These will be set after validation if module
        self.os_config: dict | None = None
        self.actions: list[dict] | None = None

    def add_error(self, error: str):
        self.errors.append(ValidationError(error))

    def is_valid(self) -> bool:
        return len(self.errors) == 0

    def to_validation_dict(self) -> dict:
        result = {"path": self.path, "valid": self.is_valid()}
        if not self.is_valid():
            result["errors"] = [e.to_dict() for e in self.errors]
        return result


# --- Discovery

def discover_configs(base_dir: str) -> list[ConfigFile]:
    """Recursively walk base_dir and discover rig config files."""
    configs = []

    for root, dirs, files in os.walk(base_dir):
        # Skip .git directories
        dirs[:] = [d for d in dirs if d != ".git"]

        for file in files:
            ext = Path(file).suffix.lower()
            if ext not in (".yaml", ".yml", ".json"):
                continue

            file_path = os.path.join(root, file)
            rel_path = os.path.relpath(file_path, base_dir)

            try:
                with open(file_path, "r") as f:
                    raw = yaml.safe_load(f) or {}
            except Exception as e:
                config = ConfigFile(rel_path, {})
                config.add_error(f"parse error: {e}")
                configs.append(config)
                continue

            abs_dir = os.path.dirname(file_path)
            config = ConfigFile(rel_path, raw, module_dir=abs_dir)
            configs.append(config)

    return configs


# --- Validation

def validate_config(config: ConfigFile) -> list[str]:
    """
    Validate a single config. Returns list of error messages.
    Order matches specification.
    """
    errors = []
    raw = config.raw_content

    # 1. parse errors already handled in discovery

    # 2. version: must be a string
    if "version" not in raw:
        errors.append("missing 'version'")
    elif not isinstance(raw["version"], str):
        errors.append("'version' must be a string")
    elif raw["version"] != "1":
        errors.append(f"unsupported version '{raw['version']}'")

    # 3. schema required, type, recognized
    if "schema" not in raw:
        errors.append("missing 'schema'")
    elif not isinstance(raw["schema"], str):
        errors.append("'schema' must be a string")
    elif raw.get("schema") not in ("module",):
        errors.append(f"unknown schema '{raw['schema']}'")

    # 4. os type (if present)
    if "os" in raw and raw["os"] is not None and not isinstance(raw["os"], dict):
        errors.append("'os' must be an object")

    # 5. os.name required/type/value (if os exists and is dict)
    os_val = raw.get("os")
    if isinstance(os_val, dict):
        os_obj = os_val
        if "name" in os_obj:
            if not isinstance(os_obj["name"], str):
                errors.append("'os.name' must be a string")
            elif os_obj["name"] not in ("macos", "linux"):
                errors.append("'os.name' must be 'macos' or 'linux'")
        elif os_obj and any(k in os_obj for k in ("packages", "applications", "package_manager")):
            errors.append("missing 'os.name'")

    # 6. os.package_manager type/value + requirement when packages/apps present
    if isinstance(os_val, dict):
        os_obj = os_val
        has_packages = "packages" in os_obj
        has_apps = "applications" in os_obj
        pm_val = os_obj.get("package_manager")

        if pm_val is not None:
            if not isinstance(pm_val, str):
                errors.append("'os.package_manager' must be a string")
            elif pm_val not in ("brew", "apt", "yum"):
                errors.append("'os.package_manager' must be 'brew', 'apt', or 'yum'")

        if has_packages or has_apps:
            if pm_val is None:
                errors.append("'os.package_manager' is required when 'packages' or 'applications' present")

    # 7. os.packages list/type/item checks
    if isinstance(os_val, dict) and "packages" in os_val:
        pkgs = os_val["packages"]
        if not isinstance(pkgs, list):
            errors.append("'os.packages' must be a list")
        else:
            for i, item in enumerate(pkgs):
                if not isinstance(item, str):
                    errors.append(f"'os.packages[{i}]' must be a string")
                elif item == "":
                    errors.append(f"'os.packages[{i}]' must not be empty")

    # 8. os.applications list/type/item checks
    if isinstance(os_val, dict) and "applications" in os_val:
        apps = os_val["applications"]
        if not isinstance(apps, list):
            errors.append("'os.applications' must be a list")
        else:
            for i, item in enumerate(apps):
                if not isinstance(item, str):
                    errors.append(f"'os.applications[{i}]' must be a string")
                elif item == "":
                    errors.append(f"'os.applications[{i}]' must not be empty")

    # 9. actions type and item-object checks
    if "actions" in raw:
        actions = raw["actions"]
        if not isinstance(actions, list):
            errors.append("'actions' must be a list")

    # 10-13. actions[N].type/.source/.destination/.hidden/.elevated
    if "actions" in raw and isinstance(raw["actions"], list):
        for i, action in enumerate(raw["actions"]):
            if not isinstance(action, dict):
                errors.append(f"'actions[{i}]' must be an object")
                continue

            # type
            if "type" not in action:
                errors.append(f"'actions[{i}]' missing 'type'")
            elif not isinstance(action["type"], str):
                errors.append(f"'actions[{i}].type' must be a string")
            elif action["type"] not in ("link", "copy", "run"):
                errors.append(f"'actions[{i}].type' must be 'link', 'copy', or 'run'")

            # source
            if "source" not in action:
                errors.append(f"'actions[{i}]' missing 'source'")
            elif not isinstance(action["source"], str):
                errors.append(f"'actions[{i}].source' must be a string")
            elif action["source"] == "":
                errors.append(f"'actions[{i}].source' must not be empty")

            # destination (for link/copy)
            atype = action.get("type")
            dest = action.get("destination")

            if atype in ("link", "copy"):
                if dest is None:
                    errors.append(f"'actions[{i}]' missing 'destination' for type '{atype}'")
                elif not isinstance(dest, str):
                    errors.append(f"'actions[{i}].destination' must be a string")
                elif not dest.startswith(("/", "~")):
                    errors.append(f"'actions[{i}].destination' must start with '/' or '~'")
                elif action["source"] == "*" and not dest.endswith("/"):
                    errors.append(f"'actions[{i}].destination' must end with '/' for wildcard source")
            elif atype == "run":
                if dest is not None and not isinstance(dest, str):
                    errors.append(f"'actions[{i}].destination' must be a string when present")

            # hidden, elevated type checks
            for field in ("hidden", "elevated"):
                if field in action:
                    if not isinstance(action[field], bool):
                        errors.append(f"'actions[{i}].{field}' must be a boolean")

    return errors


# --- Plan building helpers

def resolve_home(path: str) -> str:
    """Replace ~ with $HOME."""
    if path.startswith("~"):
        home = os.environ.get("HOME", "")
        if path == "~":
            return home
        return home + path[1:]
    return path


def build_plan(
    configs: list[ConfigFile],
    base_dir: str,
    os_filter: str | None = None,
    module_filter: list[str] | None = None,
) -> dict:
    """
    Build the plan from validated configs.
    Modules are identified as non-root-level directories with schema: "module".
    """
    # Identify modules (first schema:module per directory, not root-level)
    module_map: dict[str, ConfigFile] = {}  # name -> ConfigFile
    seen_dirs: set[str] = set()

    for cfg in configs:
        if cfg.schema != "module":
            continue
        if cfg.module_dir is None:
            continue
        rel_dir = os.path.relpath(cfg.module_dir, base_dir)
        if rel_dir == ".":
            continue  # root-level, not a module
        if cfg.module_dir in seen_dirs:
            continue
        seen_dirs.add(cfg.module_dir)
        name = rel_dir.replace("/", "/")
        module_map[name] = cfg

    # Apply filters
    if module_filter:
        module_map = {k: v for k, v in module_map.items() if k in module_filter}

    if os_filter:
        module_map = {
            k: v for k, v in module_map.items()
            if v.os_config is None or v.os_config.get("name") == os_filter
        }

    # Build plan output
    modules_out = []

    for name in sorted(module_map.keys()):
        cfg = module_map[name]
        actions_out = []

        os_config = cfg.os_config
        manager = os_config.get("package_manager") if os_config else None

        # 1. install_package actions (sorted by package)
        if os_config and "packages" in os_config:
            for pkg in sorted(os_config["packages"]):
                actions_out.append({
                    "type": "install_package",
                    "manager": manager,
                    "package": pkg,
                })

        # 2. install_application actions (sorted by application)
        if os_config and "applications" in os_config:
            for app in sorted(os_config["applications"]):
                actions_out.append({
                    "type": "install_application",
                    "manager": manager,
                    "application": app,
                })

        # 3. File actions (config order, with wildcard expansion)
        for action in cfg.actions or []:
            source = action.get("source", "")
            if source == "*":
                # Wildcard expansion
                module_base = cfg.module_dir
                if module_base and os.path.isdir(module_base):
                    for entry in sorted(os.listdir(module_base)):
                        entry_path = os.path.join(module_base, entry)
                        if os.path.isdir(entry_path):
                            continue
                        if entry.lower().endswith((".yaml", ".yml", ".json")):
                            continue
                        if entry.startswith("."):
                            continue
                        dest_base = action.get("destination", "")
                        final_dest = dest_base + entry
                        if action.get("hidden", False) and not entry.startswith("."):
                            final_dest = os.path.dirname(dest_base) + "/." + os.path.basename(entry)
                        final_dest = resolve_home(final_dest)

                        actions_out.append({
                            "type": "link",
                            "source": entry_path,
                            "destination": final_dest,
                            "elevated": action.get("elevated", False),
                        })
            else:
                # Non-wildcard action
                atype = action["type"]
                source_rel = action["source"]
                elevated = action.get("elevated", False)

                if source_rel.startswith("/"):
                    # Invalid - don't normalize, just use as-is
                    source_abs = source_rel
                else:
                    source_abs = os.path.join(cfg.module_dir, source_rel)
                    source_abs = os.path.normpath(source_abs)

                if atype in ("link", "copy"):
                    dest = action.get("destination", "")
                    if dest:
                        dest = resolve_home(dest)
                    actions_out.append({
                        "type": atype,
                        "source": source_abs,
                        "destination": dest,
                        "elevated": elevated,
                    })
                elif atype == "run":
                    actions_out.append({
                        "type": "run",
                        "source": source_abs,
                        "elevated": elevated,
                    })

        modules_out.append({"name": name, "actions": actions_out})

    return {"modules": modules_out}


# --- CLI commands

def cmd_validate(args):
    """Implement validate command."""
    base_dir = args.dir

    raw_configs = discover_configs(base_dir)

    for cfg in raw_configs:
        errors = validate_config(cfg)
        cfg.errors = [ValidationError(e) for e in errors]

    all_valid = all(c.is_valid() for c in raw_configs)
    files_out = sorted([c.to_validation_dict() for c in raw_configs], key=lambda x: x["path"])

    result = {"valid": all_valid, "files": files_out}
    print(json.dumps(result, indent=2))
    return 0 if all_valid else 1


def cmd_plan(args):
    """Implement plan command."""
    base_dir = args.dir
    os_filter = args.os
    module_filter = args.module

    raw_configs = discover_configs(base_dir)

    # First validate all configs
    validation_errors: list[dict] = []
    for cfg in raw_configs:
        errors = validate_config(cfg)
        cfg.errors = [ValidationError(e) for e in errors]
        if not cfg.is_valid():
            validation_errors.append({
                "path": cfg.path,
                "valid": False,
                "errors": [e.to_dict() for e in cfg.errors]
            })

    # If any validation errors, report and exit
    if validation_errors:
        result = {"valid": False, "files": sorted(validation_errors, key=lambda x: x["path"])}
        print(json.dumps(result, indent=2), file=sys.stderr)
        return 1

    # Set os_config and actions for module configs
    for cfg in raw_configs:
        if cfg.schema == "module":
            cfg.os_config = cfg.raw_content.get("os")
            cfg.actions = cfg.raw_content.get("actions")

    # Check for duplicate module schemas in the same directory
    # For plan: duplicate module schema is a validation error
    dirs_with_modules: dict[str, list[ConfigFile]] = {}
    for cfg in raw_configs:
        if cfg.schema != "module" or not cfg.module_dir:
            continue
        rel_dir = os.path.relpath(cfg.module_dir, base_dir)
        if rel_dir == ".":
            continue  # root-level not a module
        if cfg.module_dir not in dirs_with_modules:
            dirs_with_modules[cfg.module_dir] = []
        dirs_with_modules[cfg.module_dir].append(cfg)

    for dir_path, configs in dirs_with_modules.items():
        if len(configs) > 1:
            rel_dir = os.path.relpath(dir_path, base_dir)
            print(json.dumps({
                "valid": False,
                "files": [{
                    "path": os.path.join(rel_dir, c.path),
                    "valid": False,
                    "errors": [{"error": "duplicate 'module' schema in '" + rel_dir + "'"}]
                } for c in configs]
            }), file=sys.stderr)
            return 1

    # Check for missing source files in non-wildcard actions
    for cfg in raw_configs:
        if cfg.schema != "module" or not cfg.module_dir:
            continue
        for action in cfg.actions or []:
            if action.get("source") == "*":
                continue
            source_rel = action.get("source", "")
            if source_rel.startswith("/"):
                source_abs = source_rel
            else:
                source_abs = os.path.join(cfg.module_dir, source_rel)
            source_abs = os.path.normpath(source_abs)
            if not os.path.exists(source_abs):
                rel_dir = os.path.relpath(cfg.module_dir, base_dir)
                print(json.dumps({
                    "error": "file_not_found",
                    "details": f"module '{rel_dir}' missing source file '{source_rel}'"
                }), file=sys.stderr)
                return 1

    # Build plan
    plan = build_plan(raw_configs, base_dir, os_filter, module_filter)
    print(json.dumps(plan, indent=2))
    return 0


# --- Main

def main():
    parser = argparse.ArgumentParser(prog="rig.py")
    subparsers = parser.add_subparsers(dest="command")

    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("dir", help="Directory to validate")

    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("dir", help="Directory to plan")
    plan_parser.add_argument("--os", help="Filter by OS name")
    plan_parser.add_argument("--module", nargs="*", help="Filter by module names")

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
