#!/usr/bin/env python3

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class ForgeError(Exception):
    """Exception raised for Forge errors."""
    def __init__(self, error_type: str, detail: Any):
        self.error_type = error_type
        self.detail = detail
        super().__init__(json.dumps({"error": error_type, "detail": detail}))


def exit_error(error_type: str, detail: Any) -> None:
    """Write an error to stdout and exit with code 1."""
    print(json.dumps({"error": error_type, "detail": detail}))
    sys.exit(1)


def generate_uuid() -> str:
    """Generate a UUID v4 in lowercase hyphenated format."""
    return str(uuid.uuid4()).lower()


def get_timestamp() -> str:
    """Get current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(data_dir: Path, filename: str) -> Dict[str, Any]:
    """Load JSON data from the data directory."""
    filepath = data_dir / filename
    if not filepath.exists():
        return {}
    try:
        with open(filepath, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def save_json(data_dir: Path, filename: str, data: Dict[str, Any]) -> None:
    """Save JSON data to the data directory."""
    data_dir.mkdir(parents=True, exist_ok=True)
    filepath = data_dir / filename
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2)


# Unit field names for validation and filtering
UNIT_STRING_FIELDS = {"category", "manufacturer", "host"}
UNIT_INTEGER_FIELDS = {"capacity"}
UNIT_NULLABLE_FIELDS = {"parent_uuid"}
UNIT_ARRAY_FIELDS = {"module_uuids"}
UNIT_SYSTEM_FIELDS = {"uuid", "status", "module_uuids", "created_at", "reserved_capacity"}
UNIT_ALL_FIELDS = UNIT_STRING_FIELDS | UNIT_INTEGER_FIELDS | UNIT_NULLABLE_FIELDS | UNIT_ARRAY_FIELDS | UNIT_SYSTEM_FIELDS


def validate_unit_creation(input_data: Any) -> List[str]:
    """Validate unit creation input. Returns list of error messages."""
    errors = []

    if not isinstance(input_data, dict):
        errors.append("Input must be a JSON object")
        return errors

    # Check for system-assigned fields in payload
    for field in UNIT_SYSTEM_FIELDS:
        if field in input_data:
            errors.append(f"Field '{field}' is system-assigned and cannot be provided")

    if "category" not in input_data:
        errors.append("Missing required field 'category'")
    elif not isinstance(input_data["category"], str) or input_data["category"] == "":
        errors.append("Field 'category' must be a non-empty string")

    if "manufacturer" not in input_data:
        errors.append("Missing required field 'manufacturer'")
    elif not isinstance(input_data["manufacturer"], str) or input_data["manufacturer"] == "":
        errors.append("Field 'manufacturer' must be a non-empty string")

    if "host" not in input_data:
        errors.append("Missing required field 'host'")
    elif not isinstance(input_data["host"], str) or input_data["host"] == "":
        errors.append("Field 'host' must be a non-empty string")

    if "capacity" not in input_data:
        errors.append("Missing required field 'capacity'")
    else:
        capacity = input_data["capacity"]
        # Boolean values must be explicitly rejected (bool is a subclass of int in Python)
        if isinstance(capacity, bool):
            errors.append("Field 'capacity' must be an integer, not a boolean")
        elif not isinstance(capacity, int):
            errors.append("Field 'capacity' must be an integer")
        elif capacity < 0:
            errors.append("Field 'capacity' must be a non-negative integer")

    if "parent_uuid" in input_data:
        parent_uuid = input_data["parent_uuid"]
        if not isinstance(parent_uuid, str):
            errors.append("Field 'parent_uuid' must be a string")
        elif parent_uuid == "":
            errors.append("Field 'parent_uuid' cannot be an empty string")

    # Validate optional capabilities field
    if "parent_uuid" not in errors and "capabilities" in input_data:
        capabilities = input_data["capabilities"]
        if not isinstance(capabilities, list):
            errors.append("Field 'capabilities' must be an array")
        else:
            for i, cap in enumerate(capabilities):
                if not isinstance(cap, str):
                    errors.append(f"Field 'capabilities[{i}]' must be a string")
                elif cap == "":
                    errors.append(f"Field 'capabilities[{i}]' cannot be an empty string")

    return errors


def cmd_unit_create(data_dir: Path, input_data: Dict[str, Any]) -> Dict[str, Any]:
    """Create a new unit."""
    errors = validate_unit_creation(input_data)
    if errors:
        exit_error("validation_error", errors)

    category = input_data["category"]
    manufacturer = input_data["manufacturer"]
    host = input_data["host"]
    capacity = input_data["capacity"]
    parent_uuid = input_data.get("parent_uuid")
    capabilities = sorted(input_data.get("capabilities", []))

    # Check if parent_uuid references an existing unit
    if parent_uuid is not None:
        units = load_json(data_dir, "units.json")
        if parent_uuid not in units:
            exit_error("not_found", f"Unit with uuid '{parent_uuid}' not found")

    # Create new unit
    unit_uuid = generate_uuid()
    unit = {
        "uuid": unit_uuid,
        "category": category,
        "manufacturer": manufacturer,
        "host": host,
        "status": "active",
        "parent_uuid": parent_uuid,
        "module_uuids": [],
        "capacity": capacity,
        "capabilities": capabilities,
        "reserved_capacity": 0,
        "created_at": get_timestamp()
    }

    # Save unit
    units = load_json(data_dir, "units.json")
    units[unit_uuid] = unit
    save_json(data_dir, "units.json", units)

    return unit


def parse_generic_filter(filter_str: str) -> Tuple[Optional[str], Optional[str]]:
    """Parse a generic filter string of form 'key=value'. Returns (key, value) or (None, None) if invalid."""
    if "=" not in filter_str:
        return None, None
    # Split on first = only
    parts = filter_str.split("=", 1)
    return parts[0], parts[1]


def matches_filter(unit: Dict[str, Any], field: str, value: str) -> bool:
    """Check if a unit matches a generic filter. Returns False for nonexistent fields or array fields."""
    # Array fields are not filterable via generic expressions
    if field in UNIT_ARRAY_FIELDS:
        return False

    # Check if field exists in unit
    if field not in unit:
        return False

    unit_value = unit[field]

    # Handle null fields - match literal "null"
    if unit_value is None:
        return value == "null"

    # String fields - exact match (case-sensitive)
    if field in UNIT_STRING_FIELDS:
        return unit_value == value

    # Integer fields - compare against string representation
    if field in UNIT_INTEGER_FIELDS:
        return str(unit_value) == value

    # For other fields (uuid, status, parent_uuid), convert to string for comparison
    return str(unit_value) == value


def cmd_unit_list(
    data_dir: Path,
    category: Optional[str] = None,
    manufacturer: Optional[str] = None,
    host: Optional[str] = None,
    filters: Optional[List[str]] = None
) -> List[Dict[str, Any]]:
    """List all units with optional filters."""
    units = load_json(data_dir, "units.json")
    unit_list = list(units.values())

    # Apply named filters
    if category is not None:
        unit_list = [u for u in unit_list if u.get("category") == category]

    if manufacturer is not None:
        unit_list = [u for u in unit_list if u.get("manufacturer") == manufacturer]

    if host is not None:
        unit_list = [u for u in unit_list if u.get("host") == host]

    # Apply generic filters
    if filters:
        for filter_str in filters:
            field, value = parse_generic_filter(filter_str)
            if field is None or value is None:
                # Invalid filter format - should be caught by argparse validation
                exit_error("validation_error", f"Invalid filter format: '{filter_str}'")
            unit_list = [u for u in unit_list if matches_filter(u, field, value)]

    # Sort by: host (asc), category (asc), created_at (asc), uuid (asc)
    unit_list.sort(key=lambda u: (u["host"], u["category"], u["created_at"], u["uuid"]))

    return unit_list


def cmd_unit_get(data_dir: Path, uuid_str: str) -> Dict[str, Any]:
    """Get a unit by UUID."""
    units = load_json(data_dir, "units.json")

    if uuid_str not in units:
        exit_error("not_found", f"Unit with uuid '{uuid_str}' not found")

    return units[uuid_str]


def cmd_unit_activate(data_dir: Path, uuid_str: str) -> Dict[str, Any]:
    """Activate a unit (transition from inactive to active)."""
    units = load_json(data_dir, "units.json")

    if uuid_str not in units:
        exit_error("not_found", f"Unit with uuid '{uuid_str}' not found")

    unit = units[uuid_str]

    if unit["status"] != "inactive":
        exit_error("invalid_transition", f"Unit with uuid '{uuid_str}' is not inactive")

    # Transition to active
    unit["status"] = "active"
    unit["reserved_capacity"] = 0
    # Capabilities are already stored, they will be included when activated

    save_json(data_dir, "units.json", units)

    return unit


def cmd_unit_deactivate(data_dir: Path, uuid_str: str) -> Dict[str, Any]:
    """Deactivate a unit (transition from active to inactive)."""
    units = load_json(data_dir, "units.json")

    if uuid_str not in units:
        exit_error("not_found", f"Unit with uuid '{uuid_str}' not found")

    unit = units[uuid_str]

    if unit["status"] != "active":
        exit_error("invalid_transition", f"Unit with uuid '{uuid_str}' is not active")

    # Transition to inactive
    unit["status"] = "inactive"
    unit["reserved_capacity"] = unit["capacity"]
    # Capabilities are removed from the ledger by not being included

    save_json(data_dir, "units.json", units)

    return unit


def cmd_ledger_capabilities(data_dir: Path) -> List[str]:
    """Get set of capabilities from all active units."""
    units = load_json(data_dir, "units.json")

    capabilities = set()
    for unit in units.values():
        if unit["status"] == "active":
            capabilities.update(unit.get("capabilities", []))

    return sorted(list(capabilities))


def main():
    parser = argparse.ArgumentParser(
        description="Forge - Unit Inventory CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--data-dir", required=True, help="Directory for persistent state")

    # Top-level subcommands
    subparsers = parser.add_subparsers(dest="command", required=True)

    # ledger subcommand
    ledger_parser = subparsers.add_parser("ledger", help="Ledger operations")
    ledger_subparsers = ledger_parser.add_subparsers(dest="subcmd", required=True)

    # ledger capabilities
    ledger_caps_cmd = ledger_subparsers.add_parser("capabilities", help="List available capabilities")
    ledger_caps_cmd.set_defaults(func=lambda args, data_dir: cmd_ledger_capabilities(data_dir))

    # unit subcommand
    unit_parser = subparsers.add_parser("unit", help="Unit management")
    unit_subparsers = unit_parser.add_subparsers(dest="subcmd", required=True)

    # unit create
    create_cmd = unit_subparsers.add_parser("create", help="Create a new unit")
    create_cmd.set_defaults(func=lambda args, data_dir: cmd_unit_create(data_dir, json.load(sys.stdin)))

    # unit list
    list_cmd = unit_subparsers.add_parser("list", help="List all units")
    list_cmd.add_argument("--category", dest="category", help="Exact match on category")
    list_cmd.add_argument("--manufacturer", dest="manufacturer", help="Exact match on manufacturer")
    list_cmd.add_argument("--host", dest="host", help="Exact match on host")
    list_cmd.add_argument("--filter", dest="filters", action="append", default=[],
                          help="Generic field filter (key=value), may appear multiple times")
    list_cmd.set_defaults(func=lambda args, data_dir: cmd_unit_list(
        data_dir, args.category, args.manufacturer, args.host, args.filters
    ))

    # unit get
    get_cmd = unit_subparsers.add_parser("get", help="Get a unit by UUID")
    get_cmd.add_argument("uuid", help="Unit UUID")
    get_cmd.set_defaults(func=lambda args, data_dir: cmd_unit_get(data_dir, args.uuid))

    # unit activate
    activate_cmd = unit_subparsers.add_parser("activate", help="Activate a unit")
    activate_cmd.add_argument("uuid", help="Unit UUID")
    activate_cmd.set_defaults(func=lambda args, data_dir: cmd_unit_activate(data_dir, args.uuid))

    # unit deactivate
    deactivate_cmd = unit_subparsers.add_parser("deactivate", help="Deactivate a unit")
    deactivate_cmd.add_argument("uuid", help="Unit UUID")
    deactivate_cmd.set_defaults(func=lambda args, data_dir: cmd_unit_deactivate(data_dir, args.uuid))

    args = parser.parse_args()

    data_dir = Path(args.data_dir)

    try:
        result = args.func(args, data_dir)
        print(json.dumps(result, indent=2))
        sys.exit(0)
    except json.JSONDecodeError:
        exit_error("validation_error", "Invalid JSON input")
    except ForgeError as e:
        exit_error(e.error_type, e.detail)


if __name__ == "__main__":
    main()
