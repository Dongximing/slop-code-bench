#!/usr/bin/env python3
"""Forge CLI tool for managing blueprint resource templates."""
import os
import sys
import json
import uuid
import datetime
import argparse
from pathlib import Path
from typing import Any

class Error(Exception):
    """Base exception for Forge errors."""
    def __init__(self, category: str, detail: str):
        self.category = category
        self.detail = detail
    def to_json(self) -> str:
        return json.dumps({"error": self.category, "detail": self.detail})

class ValidationError(Error):
    def __init__(self, detail: str):
        super().__init__("validation_error", detail)

class NotFoundError(Error):
    def __init__(self, detail: str):
        super().__init__("not_found", detail)

class ConflictError(Error):
    def __init__(self, detail: str):
        super().__init__("conflict", detail)

class BlueprintStore:
    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.index_file = self.data_dir / "index.json"
        self.blueprints_dir = self.data_dir / "blueprints"
        self.blueprints_dir.mkdir(parents=True, exist_ok=True)
        self._load_index()

    def _load_index(self):
        self.index = {}
        if self.index_file.exists():
            with open(self.index_file, "r") as f:
                self.index = json.load(f)
        self.name_to_uuid = {bp["name"]: uuid_str for uuid_str, bp in self.index.items()}

    def _write_index(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with open(self.index_file, "w") as f:
            json.dump(self.index, f, sort_keys=True)

    def _save_blueprint_file(self, uuid_str: str, blueprint: dict):
        bp_file = self.blueprints_dir / f"{uuid_str}.json"
        bp_file.write_text(json.dumps(blueprint, sort_keys=False))

    def _delete_blueprint_file(self, uuid_str: str):
        bp_file = self.blueprints_dir / f"{uuid_str}.json"
        if bp_file.exists():
            bp_file.unlink()

    def create(self, name: str, requirement_sets: list) -> dict:
        if name in self.name_to_uuid:
            raise ConflictError(f"Blueprint with name '{name}' already exists.")
        uuid_str = str(uuid.uuid4()).lower()
        created_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        blueprint = {
            "uuid": uuid_str,
            "name": name,
            "requirement_sets": requirement_sets,
            "created_at": created_at,
        }
        self.index[uuid_str] = blueprint
        self.name_to_uuid[name] = uuid_str
        self._write_index()
        self._save_blueprint_file(uuid_str, blueprint)
        return blueprint

    def get(self, uuid_str: str) -> dict:
        if uuid_str not in self.index:
            raise NotFoundError(f"No blueprint found with uuid '{uuid_str}'.")
        return self.index[uuid_str].copy()

    def get_by_name(self, name: str) -> dict:
        if name not in self.name_to_uuid:
            raise NotFoundError(f"No blueprint found with name '{name}'.")
        uuid_str = self.name_to_uuid[name]
        return self.index[uuid_str].copy()

    def list_all(self) -> list:
        blueprints = list(self.index.values())
        blueprints.sort(key=lambda bp: (bp["created_at"], bp["name"], bp["uuid"]))
        return blueprints

    def delete_by_uuid(self, uuid_str: str) -> int:
        if uuid_str not in self.index:
            raise NotFoundError(f"No blueprint found with uuid '{uuid_str}'.")
        bp = self.index[uuid_str]
        del self.index[uuid_str]
        del self.name_to_uuid[bp["name"]]
        self._write_index()
        self._delete_blueprint_file(uuid_str)
        return 1

    def delete_by_names(self, names: list) -> int:
        unresolved = [n for n in names if n not in self.name_to_uuid]
        if unresolved:
            raise NotFoundError("Unresolved names: " + ",".join(unresolved))
        deleted = 0
        for name in names:
            uuid_str = self.name_to_uuid[name]
            bp = self.index[uuid_str]
            del self.index[uuid_str]
            del self.name_to_uuid[name]
            self._delete_blueprint_file(uuid_str)
            deleted += 1
        self._write_index()
        return deleted

def validate_requirement_set(rs: Any, index: int) -> list[str]:
    """Validate a requirement set. Returns list of error messages."""
    errors = []
    if not isinstance(rs, dict):
        errors.append(f"requirement_sets[{index}] must be an object.")
        return errors

    supported_keys = {"resource_type", "resource_count", "capabilities"}
    extra_keys = set(rs.keys()) - supported_keys
    for key in extra_keys:
        errors.append(f"requirement_sets[{index}] contains unsupported key '{key}'.")

    # resource_type required and string
    if "resource_type" not in rs:
        errors.append(f"requirement_sets[{index}] missing required field 'resource_type'.")
    elif not isinstance(rs["resource_type"], str):
        errors.append(f"requirement_sets[{index}] field 'resource_type' must be a string.")

    # resource_count required and positive integer
    if "resource_count" not in rs:
        errors.append(f"requirement_sets[{index}] missing required field 'resource_count'.")
    elif not isinstance(rs["resource_count"], int):
        # Boolean is not an integer
        errors.append(f"requirement_sets[{index}] field 'resource_count' must be an integer.")
    elif rs["resource_count"] < 1:
        errors.append(f"requirement_sets[{index}] field 'resource_count' must be >= 1.")

    # capabilities optional but must be array of strings
    if "capabilities" in rs:
        if not isinstance(rs["capabilities"], list):
            errors.append(f"requirement_sets[{index}] field 'capabilities' must be an array.")
        else:
            for i, cap in enumerate(rs["capabilities"]):
                if not isinstance(cap, str):
                    errors.append(f"requirement_sets[{index}] field 'capabilities'[{i}] must be a string.")
    return errors

def validate_blueprint_input(data: Any) -> list[str]:
    """Validate blueprint creation input. Returns list of error messages."""
    errors = []
    if not isinstance(data, dict):
        return ["Input must be a JSON object."]

    # name must be present and non-empty string
    if "name" not in data:
        errors.append("name is required.")
    elif not isinstance(data["name"], str):
        errors.append("name must be a string.")
    elif data["name"] == "":
        errors.append("name must be non-empty.")

    # requirement_sets must be present and non-empty array
    if "requirement_sets" not in data:
        errors.append("requirement_sets is required.")
    elif not isinstance(data["requirement_sets"], list):
        errors.append("requirement_sets must be an array.")
    elif len(data["requirement_sets"]) == 0:
        errors.append("requirement_sets must be non-empty.")

    # Validate each requirement set
    if isinstance(data.get("requirement_sets"), list):
        for i, rs in enumerate(data["requirement_sets"]):
            errors.extend(validate_requirement_set(rs, i))

    return errors

def parse_args(argv):
    """Parse forge.py arguments manually to handle --names flag correctly."""
    data_dir = None
    i = 1
    while i < len(argv):
        if argv[i] == "--data-dir":
            i += 1
            if i >= len(argv):
                print(json.dumps({"error": "validation_error", "detail": "Missing value for --data-dir."}))
                sys.exit(1)
            data_dir = argv[i]
            i += 1
        elif argv[i] in ("-h", "--help"):
            print("Usage: forge.py --data-dir <path> blueprint <command> [arguments]")
            sys.exit(0)
        else:
            break

    if data_dir is None:
        print(json.dumps({"error": "validation_error", "detail": "--data-dir is required."}))
        sys.exit(1)

    # Remaining args after --data-dir
    remaining = argv[i:]
    if len(remaining) < 1:
        print(json.dumps({"error": "validation_error", "detail": "Missing blueprint command."}))
        sys.exit(1)

    if remaining[0] != "blueprint":
        print(json.dumps({"error": "validation_error", "detail": "Unknown command structure."}))
        sys.exit(1)

    if len(remaining) < 2:
        print(json.dumps({"error": "validation_error", "detail": "Missing blueprint subcommand."}))
        sys.exit(1)

    command = remaining[1]
    args_list = remaining[2:] if len(remaining) > 2 else []

    return data_dir, command, args_list

def main():
    data_dir, command, args_list = parse_args(sys.argv)
    store = BlueprintStore(data_dir)

    # CREATE
    if command == "create":
        try:
            raw = sys.stdin.read()
        except Exception:
            print(json.dumps({"error": "validation_error", "detail": "stdin is not valid JSON."}))
            sys.exit(1)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            print(json.dumps({"error": "validation_error", "detail": "stdin is not valid JSON."}))
            sys.exit(1)

        errors = validate_blueprint_input(data)
        if errors:
            detail = " ".join(errors)
            print(json.dumps({"error": "validation_error", "detail": detail}))
            sys.exit(1)

        # Strip out any extra top-level keys; only name and requirement_sets are used
        name = data["name"]
        # Build a clean requirement_sets preserving only the allowed keys with correct types
        clean_rsets = []
        for rs in data["requirement_sets"]:
            clean = {}
            if "resource_type" in rs:
                clean["resource_type"] = rs["resource_type"]
            if "resource_count" in rs:
                clean["resource_count"] = rs["resource_count"]
            if "capabilities" in rs:
                clean["capabilities"] = rs["capabilities"]
            clean_rsets.append(clean)

        try:
            blueprint = store.create(name, clean_rsets)
            print(json.dumps(blueprint))
            sys.exit(0)
        except ConflictError as e:
            print(e.to_json())
            sys.exit(1)

    # LIST
    elif command == "list":
        blueprints = store.list_all()
        print(json.dumps(blueprints))
        sys.exit(0)

    # GET
    elif command == "get":
        if len(args_list) != 1:
            print(json.dumps({"error": "validation_error", "detail": "Usage: blueprint get <uuid>"}))
            sys.exit(1)
        uuid_str = args_list[0]
        try:
            blueprint = store.get(uuid_str)
            print(json.dumps(blueprint))
            sys.exit(0)
        except NotFoundError as e:
            print(e.to_json())
            sys.exit(1)

    # DELETE
    elif command == "delete":
        if len(args_list) >= 1 and args_list[0] == "--names":
            # Next argument is the comma-separated list
            if len(args_list) < 2:
                print(json.dumps({"error": "validation_error", "detail": "Usage: blueprint delete --names <name1,name2,...>"}))
                sys.exit(1)
            names_csv = args_list[1]
            if names_csv == "":
                names = []
            else:
                names = names_csv.split(",")
            try:
                deleted = store.delete_by_names(names)
                print(json.dumps({"deleted": deleted}))
                sys.exit(0)
            except NotFoundError as e:
                print(e.to_json())
                sys.exit(1)
        elif len(args_list) == 1:
            uuid_str = args_list[0]
            try:
                deleted = store.delete_by_uuid(uuid_str)
                print(json.dumps({"deleted": deleted}))
                sys.exit(0)
            except NotFoundError as e:
                print(e.to_json())
                sys.exit(1)
        else:
            print(json.dumps({"error": "validation_error", "detail": "Usage: blueprint delete <uuid> or blueprint delete --names <name1,name2,...>"}))
            sys.exit(1)

    else:
        print(json.dumps({"error": "validation_error", "detail": f"Unknown command: {command}"}))
        sys.exit(1)

if __name__ == "__main__":
    main()
