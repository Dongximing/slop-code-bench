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
UNIT_SYSTEM_FIELDS = {"uuid", "status", "module_uuids", "created_at"}
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

    # Validate category - required non-empty string
    if "category" not in input_data:
        errors.append("Missing required field 'category'")
    elif not isinstance(input_data["category"], str) or input_data["category"] == "":
        errors.append("Field 'category' must be a non-empty string")

    # Validate manufacturer - required non-empty string
    if "manufacturer" not in input_data:
        errors.append("Missing required field 'manufacturer'")
    elif not isinstance(input_data["manufacturer"], str) or input_data["manufacturer"] == "":
        errors.append("Field 'manufacturer' must be a non-empty string")

    # Validate host - required non-empty string
    if "host" not in input_data:
        errors.append("Missing required field 'host'")
    elif not isinstance(input_data["host"], str) or input_data["host"] == "":
        errors.append("Field 'host' must be a non-empty string")

    # Validate capacity - required non-negative integer
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

    # Validate parent_uuid - optional but must be non-empty string if provided
    if "parent_uuid" in input_data:
        parent_uuid = input_data["parent_uuid"]
        if not isinstance(parent_uuid, str):
            errors.append("Field 'parent_uuid' must be a string")
        elif parent_uuid == "":
            errors.append("Field 'parent_uuid' cannot be an empty string")

    return errors


def cmd_unit_create(data_dir: Path, input_data: Dict[str, Any]) -> Dict[str, Any]:
    """Create a new unit."""
    # Validate input
    errors = validate_unit_creation(input_data)
    if errors:
        exit_error("validation_error", errors)

    category = input_data["category"]
    manufacturer = input_data["manufacturer"]
    host = input_data["host"]
    capacity = input_data["capacity"]
    parent_uuid = input_data.get("parent_uuid")

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


def main():
    parser = argparse.ArgumentParser(
        description="Forge - Unit Inventory CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--data-dir", required=True, help="Directory for persistent state")

    # Top-level subcommands
    subparsers = parser.add_subparsers(dest="command", required=True)

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
