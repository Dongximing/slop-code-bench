#!/usr/bin/env python3
"""Local vault for media-platform metadata and records tracked-field history."""

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from typing import Any


CATALOG_VERSION = 3
SYNC_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S"


def get_sync_timestamp() -> str:
    """Return current UTC timestamp in YYYY-MM-DDTHH:MM:SS format."""
    return datetime.now(timezone.utc).strftime(SYNC_TIMESTAMP_FORMAT)


def normalize_datetime(dt_str: str) -> str:
    """Normalize datetime string to YYYY-MM-DDTHH:MM:SS format."""
    # Try full ISO format first
    try:
        dt = datetime.fromisoformat(dt_str)
        return dt.strftime(SYNC_TIMESTAMP_FORMAT)
    except ValueError:
        pass
    # Try date-only format
    try:
        dt = datetime.strptime(dt_str, "%Y-%m-%d")
        return dt.strftime(SYNC_TIMESTAMP_FORMAT)
    except ValueError:
        return dt_str  # Assume already normalized


def validate_source_entry(entry: dict[str, Any]) -> tuple[bool, str]:
    """Validate a source entry has all required fields with correct types."""
    required_fields = {
        "id": str,
        "published": str,
        "width": int,
        "height": int,
        "title": str,
        "description": str,
        "views": int,
        "likes": (int, type(None)),
        "preview": str,
    }

    for field, expected_type in required_fields.items():
        if field not in entry:
            return False, f"Missing required field: {field}"
        value = entry[field]
        if expected_type == type(None):
            if value is not None:
                return False, f"Field {field} must be null or integer"
        elif not isinstance(value, expected_type):
            # Special case for likes which can be int or None
            if field == "likes" and expected_type in (int, type(None)):
                if not (isinstance(value, int) or value is None):
                    return False, f"Field {field} must be int or null"
            else:
                return False, f"Field {field} must be {expected_type.__name__}"
    return True, ""


def get_history_for_field(entry: dict[str, Any], field: str) -> dict[str, str]:
    """Get the history object for a tracked field."""
    history = entry.get(field, {})
    if not isinstance(history, dict):
        history = {}
    return history


def get_current_value(history: dict[str, Any]) -> Any:
    """Get the current value from a history object (latest timestamp)."""
    if not history:
        return None
    latest_timestamp = max(history.keys())
    return history.get(latest_timestamp)


def resolve_timestamp_conflict(history: dict[str, Any], desired_timestamp: str) -> str:
    """Ensure timestamp is strictly later than existing entries."""
    if desired_timestamp not in history:
        return desired_timestamp
    # Advance to a strictly later second
    dt = datetime.strptime(desired_timestamp, SYNC_TIMESTAMP_FORMAT)
    while desired_timestamp in history:
        dt = datetime.fromtimestamp(dt.timestamp() + 1)
        desired_timestamp = dt.strftime(SYNC_TIMESTAMP_FORMAT)
    return desired_timestamp


def update_history(
    history: dict[str, Any], timestamp: str, new_value: Any
) -> tuple[dict[str, Any], bool]:
    """Update a history object with new value at timestamp. Returns updated history and whether a change was made."""
    current = get_current_value(history)
    # For None comparison, treat it as regular value
    if current == new_value:
        return history, False

    timestamp = resolve_timestamp_conflict(history, timestamp)
    history[timestamp] = new_value
    return history, True


def ensure_history_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Ensure all tracked fields exist as history objects."""
    tracked_fields = ["title", "description", "views", "likes", "preview", "removed"]
    for field in tracked_fields:
        if field not in entry or not isinstance(entry.get(field), dict):
            entry[field] = {}
    return entry


def calculate_sort_key(entry: dict[str, Any]) -> tuple[int, int, str]:
    """Calculate sort key for entry ordering: newest published first, then by id."""
    published_str = entry.get("published", "")
    try:
        dt = datetime.strptime(published_str, SYNC_TIMESTAMP_FORMAT)
        timestamp = int(dt.timestamp())
    except ValueError:
        timestamp = 0
    entry_id = entry.get("id", "")
    return (-timestamp, entry.get("width", 0), entry_id)


def sort_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort entries by newest published first, then by id."""
    return sorted(entries, key=calculate_sort_key)


def create_empty_catalog(source: str) -> dict[str, Any]:
    """Create a new empty catalog."""
    return {
        "version": CATALOG_VERSION,
        "source": source,
        "episodes": [],
        "streams": [],
        "clips": [],
    }


def detect_catalog_version(catalog: dict[str, Any]) -> int:
    """Detect and validate catalog version."""
    if "version" not in catalog:
        print("Error: Missing 'version' field in catalog", file=sys.stderr)
        sys.exit(1)

    version = catalog["version"]
    if not isinstance(version, int):
        print("Error: 'version' field must be an integer", file=sys.stderr)
        sys.exit(1)

    if version < 1 or version > CATALOG_VERSION:
        print(f"Error: Unsupported catalog version: {version}", file=sys.stderr)
        sys.exit(1)

    return version


def convert_unix_epoch_to_iso(history: dict[str, Any]) -> dict[str, Any]:
    """Convert Unix epoch string keys to ISO 8601 format."""
    converted = {}
    for key, value in history.items():
        try:
            # Try to parse as Unix epoch
            epoch = int(key)
            iso_key = datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
                SYNC_TIMESTAMP_FORMAT
            )
            converted[iso_key] = value
        except (ValueError, OSError):
            # Not a valid epoch, keep as-is
            converted[key] = value
    return converted


def migrate_v1_to_v3(catalog: dict[str, Any], migration_timestamp: str) -> dict[str, Any]:
    """Convert v1 catalog to v3 format."""
    # Validate required fields
    if "version" not in catalog or catalog["version"] != 1:
        raise ValueError("Invalid v1 catalog")
    if "source_id" not in catalog:
        raise ValueError("v1 catalog missing 'source_id'")
    if "entries" not in catalog:
        raise ValueError("v1 catalog missing 'entries'")

    source_id = catalog["source_id"]
    if not isinstance(source_id, str):
        raise ValueError("'source_id' must be a string")

    # Convert source_id to source URL
    source_url = f"https://media.example.com/channel/{source_id}"

    # Migrate entries: all go to episodes
    episodes = []
    for entry in catalog.get("entries", []):
        if not isinstance(entry, dict):
            raise ValueError("v1 entry must be an object")

        # Convert history keys from Unix epoch to ISO 8601
        migrated_entry = dict(entry)
        for field in ["title", "description", "views", "likes", "preview"]:
            if field in migrated_entry and isinstance(migrated_entry[field], dict):
                migrated_entry[field] = convert_unix_epoch_to_iso(migrated_entry[field])

        # Add removed field with migration timestamp
        migrated_entry["removed"] = {migration_timestamp: False}
        # Add empty annotations
        migrated_entry["annotations"] = []

        episodes.append(migrated_entry)

    # Build v3 catalog structure
    v3_catalog = {
        "version": 3,
        "source": source_url,
        "episodes": episodes,
        "streams": [],
        "clips": [],
    }

    return v3_catalog


def migrate_v2_to_v3(catalog: dict[str, Any], migration_timestamp: str) -> dict[str, Any]:
    """Convert v2 catalog to v3 format."""
    # Validate required fields
    if "version" not in catalog or catalog["version"] != 2:
        raise ValueError("Invalid v2 catalog")

    source_url = catalog.get("source")
    if not isinstance(source_url, str):
        raise ValueError("v2 catalog missing or invalid 'source'")

    # Preserve categories
    episodes = catalog.get("episodes", [])
    streams = catalog.get("streams", [])
    clips = catalog.get("clips", [])

    # Ensure they're lists
    if not isinstance(episodes, list):
        raise ValueError("v2 'episodes' must be an array")
    if not isinstance(streams, list):
        raise ValueError("v2 'streams' must be an array")
    if not isinstance(clips, list):
        raise ValueError("v2 'clips' must be an array")

    # Add removed and annotations to every entry in every category
    def add_migration_fields(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("v2 entry must be an object")
            migrated = dict(entry)
            migrated["removed"] = {migration_timestamp: False}
            migrated["annotations"] = []
            result.append(migrated)
        return result

    v3_catalog = {
        "version": 3,
        "source": source_url,
        "episodes": add_migration_fields(episodes),
        "streams": add_migration_fields(streams),
        "clips": add_migration_fields(clips),
    }

    return v3_catalog


def load_catalog(vault_path: str) -> tuple[dict[str, Any], bool]:
    """Load catalog from vault, handling v1/v2 migration transparently."""
    catalog_path = os.path.join(vault_path, "catalog.json")

    try:
        with open(catalog_path, "r") as f:
            catalog = json.load(f)
    except json.JSONDecodeError:
        print(f"Error: Invalid JSON in catalog.json", file=sys.stderr)
        sys.exit(1)
    except IOError:
        print(f"Error: Cannot read catalog.json", file=sys.stderr)
        sys.exit(1)

    # Detect version
    version = detect_catalog_version(catalog)

    # Migration needed?
    if version < CATALOG_VERSION:
        migration_timestamp = get_sync_timestamp()
        if version == 1:
            catalog = migrate_v1_to_v3(catalog, migration_timestamp)
        elif version == 2:
            catalog = migrate_v2_to_v3(catalog, migration_timestamp)
        return catalog, True  # Migrated

    return catalog, False  # Already v3 or higher


def read_catalog(vault_path: str) -> dict[str, Any]:
    """Read catalog from vault directory."""
    catalog_path = os.path.join(vault_path, "catalog.json")
    with open(catalog_path, "r") as f:
        return json.load(f)


def write_catalog(vault_path: str, catalog: dict[str, Any]) -> None:
    """Write catalog to vault directory, creating backup first."""
    catalog_path = os.path.join(vault_path, "catalog.json")
    backup_path = os.path.join(vault_path, "catalog.bak")

    # Create backup if catalog exists
    if os.path.exists(catalog_path):
        shutil.copy2(catalog_path, backup_path)

    # Write new catalog
    with open(catalog_path, "w") as f:
        json.dump(catalog, f)


def cmd_init(name: str, url: str) -> int:
    """Initialize a new vault."""
    vault_path = os.path.join(os.getcwd(), name)

    if os.path.exists(vault_path):
        print(f"Error: Vault directory '{name}' already exists", file=sys.stderr)
        return 1

    os.makedirs(vault_path)

    catalog = create_empty_catalog(url)
    write_catalog(vault_path, catalog)
    return 0


def fetch_source_data(source_url: str) -> dict[str, Any]:
    """Fetch and validate source data from URL."""
    import requests
    response = requests.get(source_url)
    response.raise_for_status()
    data = response.json()
    return data


def get_all_source_entries(source_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract all entries from all categories in source data."""
    all_entries = []
    for category in ["episodes", "streams", "clips"]:
        entries = source_data.get(category, [])
        for entry in entries:
            entry_copy = dict(entry)
            entry_copy["_category"] = category
            all_entries.append(entry_copy)
    return all_entries


def find_entry_by_id(entries: list[dict[str, Any]], entry_id: str) -> dict[str, Any] | None:
    """Find an entry by its id in a list."""
    for entry in entries:
        if entry.get("id") == entry_id:
            return entry
    return None


def cmd_migrate(name: str) -> int:
    """Migrate vault to v3 format."""
    vault_path = os.path.join(os.getcwd(), name)

    # Check vault exists
    if not os.path.exists(vault_path):
        print(f"Error: Vault '{name}' not found", file=sys.stderr)
        return 1

    # Check catalog.json exists
    catalog_path = os.path.join(vault_path, "catalog.json")
    if not os.path.exists(catalog_path):
        print(f"Error: Vault '{name}' not found", file=sys.stderr)
        return 1

    # Load catalog (detects version and migrates in memory)
    try:
        catalog, migrated = load_catalog(vault_path)
    except SystemExit:
        return 1

    # Already v3
    if not migrated:
        return 0

    # Write migrated catalog
    try:
        write_catalog(vault_path, catalog)
    except (IOError, OSError) as e:
        print(f"Error: Failed to write catalog: {e}", file=sys.stderr)
        return 1

    return 0


def sync_vault(name: str) -> int:
    """Sync vault with source data."""
    vault_path = os.path.join(os.getcwd(), name)

    # Check vault exists
    if not os.path.exists(vault_path):
        print(f"Error: Vault '{name}' not found", file=sys.stderr)
        return 1

    # Check catalog.json exists
    catalog_path = os.path.join(vault_path, "catalog.json")
    if not os.path.exists(catalog_path):
        print(f"Error: Vault '{name}' has invalid catalog", file=sys.stderr)
        return 1

    # Load catalog with auto-migration
    try:
        catalog, was_migrated = load_catalog(vault_path)
    except SystemExit:
        return 1

    source_url = catalog.get("source")
    if not source_url:
        print(f"Error: Vault '{name}' has invalid catalog", file=sys.stderr)
        return 1

    # Fetch source data
    try:
        source_data = fetch_source_data(source_url)
    except Exception as e:
        print(f"Error: Failed to fetch source metadata: {e}", file=sys.stderr)
        return 1

    # Validate source entries
    for category in ["episodes", "streams", "clips"]:
        entries = source_data.get(category, [])
        for i, entry in enumerate(entries):
            valid, error_msg = validate_source_entry(entry)
            if not valid:
                print(f"Error: Malformed source entry in {category}[{i}]: {error_msg}", file=sys.stderr)
                return 1

    sync_timestamp = get_sync_timestamp()

    # Build index of source entries by category and id
    source_index: dict[str, dict[str, dict[str, Any]]] = {}
    for category in ["episodes", "streams", "clips"]:
        source_index[category] = {}
        for entry in source_data.get(category, []):
            norm_entry = {
                "id": entry["id"],
                "published": normalize_datetime(entry["published"]),
                "width": entry["width"],
                "height": entry["height"],
                "title": entry["title"],
                "description": entry["description"],
                "views": entry["views"],
                "likes": entry["likes"],
                "preview": entry["preview"],
            }
            source_index[category][entry["id"]] = norm_entry

    # Process each category
    for category in ["episodes", "streams", "clips"]:
        existing_entries = catalog.get(category, [])
        source_entries = source_index[category]
        new_entries = []

        # Process existing entries
        for entry in existing_entries:
            entry = ensure_history_entry(dict(entry))
            entry_id = entry.get("id", "")

            if entry_id in source_entries:
                # Entry exists in source - update if needed
                source_entry = source_entries[entry_id]
                changed = False

                # Check each tracked field for changes
                tracked_fields = ["title", "description", "views", "likes", "preview"]
                for field in tracked_fields:
                    history = get_history_for_field(entry, field)
                    current_value = get_current_value(history)
                    new_value = source_entry[field]

                    # Check for changes
                    if current_value != new_value:
                        history, was_changed = update_history(history, sync_timestamp, new_value)
                        entry[field] = history
                        changed = True

                # Check if previously removed
                removed_history = get_history_for_field(entry, "removed")
                current_removed = get_current_value(removed_history)
                if current_removed == True:
                    removed_history, was_changed = update_history(removed_history, sync_timestamp, False)
                    entry["removed"] = removed_history
                    changed = True

                # Remove from source_index to mark as processed
                del source_entries[entry_id]
            else:
                # Entry missing from source - mark as removed
                removed_history = get_history_for_field(entry, "removed")
                removed_history, was_changed = update_history(removed_history, sync_timestamp, True)
                entry["removed"] = removed_history

            new_entries.append(entry)

        # Add new entries from source
        for entry_id, source_entry in source_entries.items():
            new_entry = {
                "id": entry_id,
                "published": source_entry["published"],
                "width": source_entry["width"],
                "height": source_entry["height"],
                "title": {sync_timestamp: source_entry["title"]},
                "description": {sync_timestamp: source_entry["description"]},
                "views": {sync_timestamp: source_entry["views"]},
                "likes": {sync_timestamp: source_entry["likes"]},
                "preview": {sync_timestamp: source_entry["preview"]},
                "removed": {sync_timestamp: False},
            }
            new_entries.append(new_entry)

        # Sort and store
        catalog[category] = sort_entries(new_entries)

    # Write updated catalog
    write_catalog(vault_path, catalog)
    return 0


def main() -> int:
    """Main entry point for mvault CLI."""
    parser = argparse.ArgumentParser(
        prog="mvault",
        description="Local vault for media-platform metadata and records tracked-field history.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # init command
    init_parser = subparsers.add_parser(
        "init", help="Initialize a new vault", description="Initialize a new vault"
    )
    init_parser.add_argument("name", help="Name of the vault")
    init_parser.add_argument("url", help="Source URL for the vault")

    # sync command
    sync_parser = subparsers.add_parser(
        "sync", help="Sync vault with source", description="Sync vault with source"
    )
    sync_parser.add_argument("name", help="Name of the vault")

    # migrate command
    migrate_parser = subparsers.add_parser(
        "migrate",
        help="Migrate vault to v3 format",
        description="Migrate legacy vault to v3 format",
    )
    migrate_parser.add_argument("name", help="Name of the vault")

    args = parser.parse_args()

    if args.command is None:
        print("usage: mvault <subcommand> [args...]", file=sys.stderr)
        return 1

    if args.command == "init":
        return cmd_init(args.name, args.url)
    elif args.command == "sync":
        return sync_vault(args.name)
    elif args.command == "migrate":
        return cmd_migrate(args.name)
    else:
        print(f"Error: Unknown command '{args.command}'", file=sys.stderr)
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
