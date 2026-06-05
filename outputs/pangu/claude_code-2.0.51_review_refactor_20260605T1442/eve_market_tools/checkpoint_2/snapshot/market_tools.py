#!/usr/bin/env python3
"""Market Tools API for industrial market analysis with reprocessing."""

import asyncio
import csv
import gzip
import io
import json
import hashlib
import secrets
import pathlib
from datetime import datetime
from typing import Any
from collections import defaultdict

from flask import Flask, Request, request

app = Flask(__name__)

# Required SDE path
SDE_PATH: pathlib.Path = None

# Data stores
price_books: dict[str, dict[int, list[dict]]] = {}  # market -> type_id -> [orders]
station_data: dict[int, dict] = {}  # location_id -> station info

# Reference data
type_names: dict[int, str] = {}  # type_id -> name
region_names: dict[int, str] = {}  # region_id -> name
region_hubs: dict[int, list[int]] = {}  # region_id -> [system_ids of hubs]

# Reprocessing data
type_ids: dict[int, dict] = {}  # type_id -> {groupID, portionSize, volume}
group_to_category: dict[int, int] = {}  # group_id -> category_id
category_names: dict[int, str] = {}  # category_id -> name
type_materials: dict[int, list[tuple[int, int]]] = {}  # type_id -> [(material_type_id, quantity)]
type_attributes: dict[int, dict[int, float]] = {}  # type_id -> {attr_id: value}

# API keys for configuration-driven yields
api_keys: dict[str, dict] = {}  # key -> {config}

# Skill name to attribute ID mapping for ore processing
# Based on SDE, attribute 182 links items to ore processing skill (3386 for ore, 3388 for gas)
ORE_PROCESSING_SKILL_ID = 3386
GAS_PROCESSING_SKILL_ID = 3388  # Not used in spec but available
REPROCESSING_SKILL_ID = 3385  # Base reprocessing skill

# Structure modifiers
STRUCTURE_MODIFIERS = {
    "npc_station": 0.00,
    "citadel": 0.00,
    "athanor": 0.02,
    "tatara": 0.055,
}

RIG_MODIFIERS = {
    "t1": 1,
    "t2": 3,
    "null": 0,
}

SECURITY_MODIFIERS = {
    "highsec": 0.00,
    "lowsec": 0.06,
    "nullsec": 0.12,
}

IMPLANT_MODIFIERS = {
    "null": 0.00,
    "RX-801": 0.01,
    "RX-802": 0.02,
    "RX-804": 0.04,
}

# Main hub definitions (based on specification)
MAIN_HUBS = {
    "jita": {"name": "Jita IV - Moon 4 - Caldari Navy Assembly Plant", "system_id": 30000121},
    "amarr": {"name": "Amarr VIII (Oris) - Emperor Family Academy", "system_id": 30000119},
    "dodixie": {"name": "Dodixie IX - Moon 20 - Federation Navy Assembly Plant", "system_id": 30000126},
    "rens": {"name": "Rens VI - Moon 8 - Brutor Tribe Treasury", "system_id": 30000123},
    "hek": {"name": "Hek VIII - Moon 12 - Boundless Creation Factory", "system_id": 30000127},
}


class MarketError(Exception):
    """Base exception for market API errors."""

    def __init__(self, error: str, details: str):
        self.error = error
        self.details = details
        super().__init__(f"{error}: {details}")


def load_sde(sde_path: pathlib.Path) -> None:
    """Load SDE reference data including reprocessing data."""
    global type_names, region_names, region_hubs, type_ids, group_to_category, category_names
    global type_materials, type_attributes

    # Load item type names and properties
    types_file = sde_path / "invTypes.csv.bz2"
    if types_file.exists():
        import bz2
        with bz2.open(types_file, "rt") as f:
            reader = csv.DictReader(f)
            for row in reader:
                type_id = int(row["typeID"])
                type_name = row["typeName"]
                type_names[type_id] = type_name
                type_ids[type_id] = {
                    'groupID': int(row['groupID']),
                    'portionSize': int(row['portionSize']),
                    'volume': float(row['volume']) if row['volume'] and row['volume'] != '0E-10' else 0.0
                }

    # Load groups with category mapping
    groups_file = sde_path / "invGroups.csv.bz2"
    if groups_file.exists():
        import bz2
        with bz2.open(groups_file, "rt") as f:
            reader = csv.DictReader(f)
            for row in reader:
                group_id = int(row['groupID'])
                category_id = int(row['categoryID'])
                group_to_category[group_id] = category_id

    # Load categories
    categories_file = sde_path / "invCategories.csv.bz2"
    if categories_file.exists():
        import bz2
        with bz2.open(categories_file, "rt") as f:
            reader = csv.DictReader(f)
            for row in reader:
                category_id = int(row['categoryID'])
                category_names[category_id] = row['categoryName']

    # Load type materials
    materials_file = sde_path / "invTypeMaterials.csv.bz2"
    if materials_file.exists():
        import bz2
        with bz2.open(materials_file, "rt") as f:
            reader = csv.DictReader(f)
            for row in reader:
                parent_id = int(row['typeID'])
                material_id = int(row['materialTypeID'])
                quantity = int(row['quantity'])
                if parent_id not in type_materials:
                    type_materials[parent_id] = []
                type_materials[parent_id].append((material_id, quantity))

    # Load type attributes for skill lookup
    attributes_file = sde_path / "dgmTypeAttributes.csv.bz2"
    if attributes_file.exists():
        import bz2
        with bz2.open(attributes_file, "rt") as f:
            reader = csv.DictReader(f)
            for row in reader:
                type_id = int(row['typeID'])
                attr_id = int(row['attributeID'])
                value_float = row['valueFloat']
                value_int = row['valueInt']
                value = float(value_float) if value_float and value_float != 'None' else float(value_int) if value_int and value_int != 'None' else 0.0
                if type_id not in type_attributes:
                    type_attributes[type_id] = {}
                type_attributes[type_id][attr_id] = value

    # Load regions
    regions_file = sde_path / "mapRegions.csv.bz2"
    if regions_file.exists():
        import bz2
        with bz2.open(regions_file, "rt") as f:
            reader = csv.DictReader(f)
            for row in reader:
                region_id = int(row['regionID'])
                region_name = row['regionName']
                region_names[region_id] = region_name

    # Load solar systems to build region->system mapping
    systems_file = sde_path / "mapSolarSystems.csv.bz2"
    region_systems: dict[int, list[int]] = {}
    if systems_file.exists():
        import bz2
        with bz2.open(systems_file, "rt") as f:
            reader = csv.DictReader(f)
            for row in reader:
                system_id = int(row['solarSystemID'])
                region_id = int(row['regionID'])
                if region_id not in region_systems:
                    region_systems[region_id] = []
                region_systems[region_id].append(system_id)

    # Build hub mapping: for each region, find which main hubs are in it
    region_hubs = {}
    for hub_key, hub_info in MAIN_HUBS.items():
        hub_system_id = hub_info["system_id"]
        for region_id, systems in region_systems.items():
            if hub_system_id in systems:
                if region_id not in region_hubs:
                    region_hubs[region_id] = []
                region_hubs[region_id].append(hub_system_id)


def get_type_name(type_id: int) -> str | None:
    """Get type name by ID."""
    return type_names.get(type_id)


def get_region_name(region_id: int) -> str | None:
    """Get region name by ID."""
    return region_names.get(region_id)


def get_type_info(type_name: str) -> tuple[dict, str | None]:
    """Get type info by name, returns (info, error)."""
    # Build lookup on first call
    if not type_ids:
        return {}, "SDE not loaded"
    type_id = None
    for tid, name in type_names.items():
        if name == type_name:
            type_id = tid
            break
    if type_id is None:
        return {}, "UNKNOWN_ITEMS"
    return type_ids.get(type_id, {}), None


def get_type_id(type_name: str) -> int | None:
    """Get type ID by name."""
    for tid, name in type_names.items():
        if name == type_name:
            return tid
    return None


def get_type_name_by_id(type_id: int) -> str | None:
    """Get type name by ID."""
    return type_names.get(type_id)


def is_reprocessable(type_id: int) -> bool:
    """Check if a type can be reprocessed (has materials)."""
    return type_id in type_materials and len(type_materials[type_id]) > 0


def get_category(type_id: int) -> int | None:
    """Get category ID for a type."""
    if type_id not in type_ids:
        return None
    group_id = type_ids[type_id]['groupID']
    return group_to_category.get(group_id)


def is_ore_or_ice(type_id: int) -> bool:
    """Check if type is ore, ice, or their compressed variants."""
    cat = get_category(type_id)
    if cat is None:
        return False
    # Category 25 = Asteroid (ore)
    # Category 4 = Material, group 422 = Gas Isotopes, group 423 = Ice Product, group 465 = Ice
    if cat == 25:
        return True
    if cat == 4:
        group_id = type_ids[type_id]['groupID']
        if group_id in [422, 423, 465]:  # Gas, Ice, Ice group
            return True
    return False


def is_gas(type_id: int) -> bool:
    """Check if type is gas (gas isotopes group)."""
    cat = get_category(type_id)
    if cat == 4:
        group_id = type_ids[type_id]['groupID']
        if group_id == 422:  # Gas Isotopes
            return True
    return False


def is_ice_or_ice_product(type_id: int) -> bool:
    """Check if type is ice or ice product."""
    cat = get_category(type_id)
    if cat == 4:
        group_id = type_ids[type_id]['groupID']
        if group_id in [423, 465]:  # Ice Product, Ice group
            return True
    return False


def is_pi_material(type_id: int) -> bool:
    """Check if type is Planetary Industry material."""
    cat = get_category(type_id)
    return cat in [41, 42, 43]  # Planetary Industry categories


def get_ore_processing_skill_level(type_id: int, skills: dict[str, int]) -> int:
    """Get the level of the ore processing skill referenced by the item."""
    # Look for attribute 182 which links to ore processing skill (3386 for ore)
    if type_id not in type_attributes:
        return 0
    attr_182 = type_attributes[type_id].get(182)
    if attr_182 is None:
        return 0
    # Attribute 182 stores the typeID of the required skill
    skill_type_id = int(attr_182)
    if skill_type_id == ORE_PROCESSING_SKILL_ID:
        return skills.get("Reprocessing", 0)
    elif skill_type_id == GAS_PROCESSING_SKILL_ID:
        return skills.get("Gas Cloud Harvesting", 0)
    # For other skills, check if they're in the skills dict
    skill_name = type_names.get(skill_type_id, "")
    if skill_name and skill_name in skills:
        return skills[skill_name]
    # Default to 0 if not specified
    return 0


def calculate_yield(api_key: str | None, efficiency_defaults: dict) -> dict[str, float]:
    """Calculate yield multipliers based on API key or defaults."""
    if api_key and api_key in api_keys:
        config = api_keys[api_key]
        return _calculate_yield_from_config(config, efficiency_defaults)
    return efficiency_defaults


def _calculate_yield_from_config(config: dict, efficiency_defaults: dict) -> dict[str, float]:
    """Calculate yield from a configuration."""
    structure = config.get('structure', {})
    skills = config.get('skills', {})
    implant = config.get('implant', 'null')

    # Get structure modifier
    struct_type = structure.get('type', 'npc_station')
    Sm = STRUCTURE_MODIFIERS.get(struct_type, 0.00)

    # Get rig modifier
    rig = structure.get('rig', 'null')
    if struct_type == 'npc_station':
        rig = 'null'
    Rm = RIG_MODIFIERS.get(rig, 0)

    # Get security modifier
    security = structure.get('security', 'highsec')
    Sec = SECURITY_MODIFIERS.get(security, 0.00)

    # Get implant modifier
    Im = IMPLANT_MODIFIERS.get(implant, 0.00)

    # Get skill levels
    R = skills.get('Reprocessing', 0)
    Re = skills.get('Reprocessing Efficiency', 0)

    # Get ore processing skill from any ore item - use 0 if not specified
    # The spec says Op is referenced by item's attribute in dgmTypeAttributes
    # We'll use the ore processing skill level from the item being processed
    Op = 0  # Default to 0

    # Calculate yields
    # Ore/Ice/Gas yield formula: Yield = (50 + Rm) * (1 + Sec) * (1 + Sm) * (1 + R*0.03) * (1 + Re*0.02) * (1 + Op*0.02)
    # Scrap yield is capped at 55

    base_ore = 50 + Rm
    ore_yield = base_ore * (1 + Sec) * (1 + Sm) * (1 + R * 0.03) * (1 + Re * 0.02) * (1 + Op * 0.02)
    ore_yield = round(ore_yield, 2) / 100  # Convert percentage to multiplier

    # Scrap yield: 50 + scrap reprocessing skill, max 55
    scrap_yield = min(55, 50 + R) / 100

    # Gas yield same as ore
    gas_yield = ore_yield

    return {
        'ore': round(ore_yield, 4),
        'gas': round(gas_yield, 4),
        'scrap': round(scrap_yield, 4)
    }


def get_efficiency_for_item(type_id: int, yields: dict[str, float]) -> float:
    """Get the appropriate efficiency multiplier for an item."""
    if is_ore_or_ice(type_id):
        return yields['ore']
    elif is_gas(type_id):
        return yields['gas']
    else:
        return yields['scrap']


def is_known_type(type_id: int) -> bool:
    """Check if type ID exists in SDE."""
    return type_id in type_names


def decompress_gzip(data: bytes) -> bytes:
    """Decompress gzipped data."""
    try:
        return gzip.decompress(data)
    except Exception as e:
        raise MarketError("INVALID_FORMAT", f"Failed to decompress data: {str(e)}")


def parse_csv_bytes(data: bytes, required_columns: list[str]) -> list[dict]:
    """Parse CSV bytes and validate required columns."""
    try:
        text = data.decode('utf-8')
        reader = csv.DictReader(io.StringIO(text))

        # Check required columns
        if reader.fieldnames is None:
            raise MarketError("INVALID_FORMAT", "CSV file has no headers")

        missing = set(required_columns) - set(reader.fieldnames)
        if missing:
            raise MarketError("INVALID_FORMAT", f"Missing required columns: {', '.join(missing)}")

        return list(reader)
    except MarketError:
        raise
    except Exception as e:
        raise MarketError("INVALID_FORMAT", f"Failed to parse CSV: {str(e)}")


def convert_value(value: str, target_type: type) -> Any:
    """Convert CSV string value to target type."""
    value = value.strip()
    if target_type == int:
        return int(float(value)) if value else 0
    elif target_type == float:
        return float(value) if value else 0.0
    elif target_type == bool:
        return value.lower() in ('true', '1', 'yes')
    else:
        return value


def process_price_row(row: dict) -> dict:
    """Process a price CSV row into order dict."""
    return {
        "order_id": int(row["order_id"]),
        "duration": int(row["duration"]),
        "is_buy_order": row["is_buy_order"].lower() in ('true', '1', 'yes'),
        "issued": row["issued"],
        "location_id": int(row["location_id"]),
        "min_volume": int(row["min_volume"]),
        "price": float(row["price"]),
        "range": row["range"],
        "system_id": int(row["system_id"]),
        "type_id": int(row["type_id"]),
        "volume_remain": int(row["volume_remain"]),
        "volume_total": int(row["volume_total"]),
    }


def process_station_row(row: dict) -> dict:
    """Process a station CSV row into station dict."""
    return {
        "location_id": int(row["location_id"]),
        "type": row["type"],
        "name": row["name"],
    }


def calculate_weighted_avg_price(orders: list[dict], volume_percent: float = 0.05, buy: bool = True) -> float:
    """Calculate volume-weighted average price for top N% of orders by price."""
    if not orders:
        return 0.0

    # Sort by price (buy: descending, sell: ascending)
    sorted_orders = sorted(orders, key=lambda x: x["price"], reverse=buy)

    # Calculate total volume
    total_volume = sum(o["volume_remain"] for o in orders)
    if total_volume == 0:
        return 0.0

    # Target volume for top N%
    target_volume = total_volume * volume_percent

    # Accumulate until we reach target
    accumulated = 0
    total_value = 0.0
    units_processed = 0

    for order in sorted_orders:
        if accumulated >= target_volume:
            break
        order_volume = order["volume_remain"]
        order_price = order["price"]

        remaining_needed = target_volume - accumulated
        take = min(order_volume, remaining_needed)

        total_value += take * order_price
        accumulated += take
        units_processed += take

    if units_processed == 0:
        return 0.0

    return total_value / units_processed


def filter_outliers(orders: list[dict]) -> list[dict]:
    """Filter outlier orders (10% highest buy or 10x lowest sell)."""
    if not orders:
        return []

    buy_orders = [o for o in orders if o["is_buy_order"]]
    sell_orders = [o for o in orders if not o["is_buy_order"]]

    filtered = []

    if buy_orders:
        # Get top 10% of buy prices (highest = best for buyers)
        sorted_buy = sorted(buy_orders, key=lambda x: x["price"], reverse=True)
        outlier_count = max(1, int(len(sorted_buy) * 0.1))
        valid_buy_prices = set()
        for o in sorted_buy[outlier_count:]:
            valid_buy_prices.add(o["price"])
        filtered.extend(o for o in buy_orders if o["price"] in valid_buy_prices)

    if sell_orders:
        # Get lowest 10% of sell prices (lowest = best for sellers)
        sorted_sell = sorted(sell_orders, key=lambda x: x["price"])
        outlier_count = max(1, int(len(sorted_sell) * 0.1))
        valid_sell_prices = set()
        for o in sorted_sell[:-outlier_count]:
            valid_sell_prices.add(o["price"])
        filtered.extend(o for o in sell_orders if o["price"] in valid_sell_prices)

    return filtered


@app.route("/v1/prices", methods=["POST"])
def ingest_prices():
    """Ingest gzipped CSV price data."""
    if not request.data:
        return json_error("INVALID_FORMAT", "No data provided")

    # Get query parameters
    market = request.args.get("market", "jita")
    mode = request.args.get("mode", "replace")
    location_id_override = request.args.get("location_id")

    # Decompress data
    try:
        csv_data = decompress_gzip(request.data)
    except MarketError as e:
        return json_error(e.error, e.details)

    # Parse CSV
    required = ["order_id", "duration", "is_buy_order", "issued", "location_id",
                "min_volume", "price", "range", "system_id", "type_id",
                "volume_remain", "volume_total"]
    try:
        rows = parse_csv_bytes(csv_data, required)
    except MarketError as e:
        return json_error(e.error, e.details)

    # Initialize market book if needed
    if market not in price_books:
        price_books[market] = {}

    count = 0
    for row in rows:
        try:
            order = process_price_row(row)
        except (ValueError, KeyError) as e:
            return json_error("INVALID_FORMAT", f"Invalid row data: {str(e)}")

        # Filter by location_id if specified
        if location_id_override:
            if str(order["location_id"]) != location_id_override:
                continue

        type_id = order["type_id"]

        if mode == "append":
            # Upsert by order_id (newer issued wins)
            if type_id not in price_books[market]:
                price_books[market][type_id] = []

            existing = [o for o in price_books[market][type_id] if o["order_id"] == order["order_id"]]
            if existing:
                # Replace if newer
                if order["issued"] > existing[0]["issued"]:
                    price_books[market][type_id].remove(existing[0])
                    price_books[market][type_id].append(order)
            else:
                price_books[market][type_id].append(order)
        else:
            # Replace mode - just add all
            if type_id not in price_books[market]:
                price_books[market][type_id] = []
            # Only add if not already present (avoid duplicates on re-ingest)
            existing_ids = {o["order_id"] for o in price_books[market][type_id]}
            if order["order_id"] not in existing_ids:
                price_books[market][type_id].append(order)

        count += 1

    return json_ok({"status": "PRICES_UPDATED", "count": count})


@app.route("/v1/stations", methods=["POST"])
def ingest_stations():
    """Ingest gzipped CSV station data."""
    if not request.data:
        return json_error("INVALID_FORMAT", "No data provided")

    # Decompress data
    try:
        csv_data = decompress_gzip(request.data)
    except MarketError as e:
        return json_error(e.error, e.details)

    # Parse CSV
    required = ["location_id", "type", "name"]
    try:
        rows = parse_csv_bytes(csv_data, required)
    except MarketError as e:
        return json_error(e.error, e.details)

    count = 0
    for row in rows:
        try:
            station = process_station_row(row)
        except (ValueError, KeyError) as e:
            return json_error("INVALID_FORMAT", f"Invalid row data: {str(e)}")

        location_id = station["location_id"]
        station_data[location_id] = station
        count += 1

    return json_ok({"status": "STATIONS_UPDATED", "count": count})


@app.route("/v1/market/<region_id>", methods=["GET"])
def get_market_region(region_id: str):
    """Get market stats for a region."""
    try:
        region_id_int = int(region_id)
    except ValueError:
        return json_error("UNKNOWN_ITEMS", f"Invalid region ID: {region_id}")

    if region_id_int not in region_names:
        return json_error("UNKNOWN_ITEMS", f"Unknown region ID: {region_id}")

    type_ids_param = request.args.get("type_ids")
    hubs_param = request.args.get("hubs")

    # Aggregate orders across all markets for this region
    # (In a real implementation, we'd know which markets belong to which region)
    # For now, we'll check all price books

    all_orders = []
    for market, type_orders in price_books.items():
        for type_id, orders in type_orders.items():
            all_orders.extend(orders)

    # Filter to relevant types if specified
    if type_ids_param:
        try:
            requested_types = [int(t) for t in type_ids_param.split(",")]
        except ValueError:
            return json_error("INVALID_FORMAT", "Invalid type_ids format")
    else:
        requested_types = None

    # Get hub system IDs for this region
    hub_systems = region_hubs.get(region_id_int, [])

    # Calculate stats
    sell_orders = []
    buy_orders = []
    sell_value = 0.0
    buy_value = 0.0
    unique_types = set()

    types_data = {}

    for order in all_orders:
        if requested_types and order["type_id"] not in requested_types:
            continue

        # Check if this order is from a hub system
        is_hub = order["system_id"] in hub_systems

        if not is_hub:
            continue

        type_name = get_type_name(order["type_id"]) or str(order["type_id"])

        if order["is_buy_order"]:
            buy_orders.append(order)
            unique_types.add(order["type_id"])
        else:
            sell_orders.append(order)
            unique_types.add(order["type_id"])

    # Apply outlier filtering
    all_hub_orders = buy_orders + sell_orders
    filtered_orders = filter_outliers(all_hub_orders)

    filtered_sell = [o for o in filtered_orders if not o["is_buy_order"]]
    filtered_buy = [o for o in filtered_orders if o["is_buy_order"]]

    # Calculate aggregate values
    if filtered_sell:
        # Use regional hub logic - find system with most sell orders
        sell_by_system: dict[int, list] = {}
        for o in filtered_sell:
            sys_id = o["system_id"]
            if sys_id not in sell_by_system:
                sell_by_system[sys_id] = []
            sell_by_system[sys_id].append(o)

        # Pick hub with most sell orders
        hub_system_id = max(sell_by_system.keys(), key=lambda s: len(sell_by_system[s]))
        hub_orders = sell_by_system[hub_system_id]

        # Calculate values
        sell_value = sum(o["price"] * o["volume_remain"] for o in hub_orders) / 1e9

        # Per-type stats
        sell_by_type: dict[int, list] = {}
        for o in hub_orders:
            if o["type_id"] not in sell_by_type:
                sell_by_type[o["type_id"]] = []
            sell_by_type[o["type_id"]].append(o)

        for type_id, type_orders in sell_by_type.items():
            type_name = get_type_name(type_id) or str(type_id)
            prices = [o["price"] for o in type_orders]
            sell_price = min(prices) if prices else None

            if type_name not in types_data:
                types_data[type_name] = {
                    "buy": None, "sell": None, "split": None,
                    "buy_orders": 0, "sell_orders": 0
                }
            types_data[type_name]["sell"] = sell_price
            types_data[type_name]["sell_orders"] = len(type_orders)

    if filtered_buy:
        buy_by_system: dict[int, list] = {}
        for o in filtered_buy:
            sys_id = o["system_id"]
            if sys_id not in buy_by_system:
                buy_by_system[sys_id] = []
            buy_by_system[sys_id].append(o)

        hub_system_id = max(buy_by_system.keys(), key=lambda s: len(buy_by_system[s]))
        hub_orders = buy_by_system[hub_system_id]

        buy_value = sum(o["price"] * o["volume_remain"] for o in hub_orders) / 1e9

        buy_by_type: dict[int, list] = {}
        for o in hub_orders:
            if o["type_id"] not in buy_by_type:
                buy_by_type[o["type_id"]] = []
            buy_by_type[o["type_id"]].append(o)

        for type_id, type_orders in buy_by_type.items():
            type_name = get_type_name(type_id) or str(type_id)
            prices = [o["price"] for o in type_orders]
            buy_price = max(prices) if prices else None

            if type_name not in types_data:
                types_data[type_name] = {
                    "buy": None, "sell": None, "split": None,
                    "buy_orders": 0, "sell_orders": 0
                }
            types_data[type_name]["buy"] = buy_price
            types_data[type_name]["buy_orders"] = len(type_orders)

    # Calculate split (midpoint)
    for type_name, data in types_data.items():
        if data["buy"] is not None and data["sell"] is not None:
            data["split"] = (data["buy"] + data["sell"]) / 2
        elif data["buy"] is not None:
            data["split"] = data["buy"]
        elif data["sell"] is not None:
            data["split"] = data["sell"]

    response = {
        "name": get_region_name(region_id_int),
        "sell_orders": len(filtered_sell),
        "buy_orders": len(filtered_buy),
        "sell_value": round(sell_value, 2),
        "buy_value": round(buy_value, 2),
        "unique_items": len(unique_types),
    }

    if type_ids_param or not requested_types:
        response["types"] = types_data

    if hubs_param:
        # Generate hub stats
        hubs_list = []
        for hub_key, hub_info in MAIN_HUBS.items():
            if hub_info["system_id"] in hub_systems:
                hub_orders_for_key = [o for o in all_hub_orders if o["system_id"] == hub_info["system_id"]]
                if hub_orders_for_key:
                    hub_sell_value = sum(o["price"] * o["volume_remain"] for o in hub_orders_for_key) / 1e9
                    station_name = hub_info["name"]
                else:
                    hub_sell_value = 0.0
                    station_name = hub_info["name"]

                hubs_list.append({
                    "station": station_name,
                    "orders": len(hub_orders_for_key),
                    "sell_value": round(hub_sell_value, 2),
                })

        # Sort by number of orders
        hubs_list.sort(key=lambda x: x["orders"], reverse=True)
        response["hubs"] = hubs_list

    return json_ok(response)


@app.route("/v1/market/<region_id>/<type_id>", methods=["GET"])
def get_market_type(region_id: str, type_id: str):
    """Get market stats for a specific type in a region."""
    try:
        region_id_int = int(region_id)
        type_id_int = int(type_id)
    except ValueError:
        return json_error("UNKNOWN_ITEMS", "Invalid ID format")

    if region_id_int not in region_names:
        return json_error("UNKNOWN_ITEMS", f"Unknown region ID: {region_id}")

    if not is_known_type(type_id_int):
        return json_error("UNKNOWN_ITEMS", f"Unknown type ID: {type_id}")

    type_name = get_type_name(type_id_int)

    # Get hub system IDs for this region
    hub_systems = region_hubs.get(region_id_int, [])

    # Collect orders for this type
    all_type_orders = []
    for market, type_orders in price_books.items():
        if type_id_int in type_orders:
            all_type_orders.extend(type_orders[type_id_int])

    # Filter to hub systems only
    hub_orders = [o for o in all_type_orders if o["system_id"] in hub_systems]

    if not hub_orders:
        return json_ok({
            "name": type_name,
            "buy": None,
            "sell": None,
            "buy_5pct": None,
            "sell_5pct": None,
            "buy_orders": 0,
            "sell_orders": 0,
            "buy_threshold": None,
            "sell_threshold": None,
            "buy_volume": 0,
            "sell_volume": 0,
            "total_value": 0.0,
        })

    # Apply outlier filtering
    filtered_orders = filter_outliers(hub_orders)

    buy_orders = [o for o in filtered_orders if o["is_buy_order"]]
    sell_orders = [o for o in filtered_orders if not o["is_buy_order"]]

    # Calculate thresholds (outlier cutoffs)
    buy_threshold = None
    sell_threshold = None

    if buy_orders:
        sorted_buy = sorted(buy_orders, key=lambda x: x["price"], reverse=True)
        outlier_count = max(1, int(len(sorted_buy) * 0.1))
        buy_threshold = sorted_buy[outlier_count - 1]["price"] if len(sorted_buy) > outlier_count else sorted_buy[-1]["price"]

    if sell_orders:
        sorted_sell = sorted(sell_orders, key=lambda x: x["price"])
        outlier_count = max(1, int(len(sorted_sell) * 0.1))
        sell_threshold = sorted_sell[len(sorted_sell) - outlier_count]["price"] if len(sorted_sell) > outlier_count else sorted_sell[0]["price"]

    # Calculate 5% volume weighted average
    buy_5pct = calculate_weighted_avg_price(buy_orders, 0.05, buy=True)
    sell_5pct = calculate_weighted_avg_price(sell_orders, 0.05, buy=False)

    # Calculate best prices
    buy_price = max((o["price"] for o in buy_orders), default=None)
    sell_price = min((o["price"] for o in sell_orders), default=None)

    # Volumes
    buy_volume = sum(o["volume_remain"] for o in buy_orders)
    sell_volume = sum(o["volume_remain"] for o in sell_orders)

    # Total value (5pct sell * sell volume) in billions
    total_value = (sell_5pct * sell_volume) / 1e9

    return json_ok({
        "name": type_name,
        "buy": round(buy_price, 2) if buy_price is not None else None,
        "sell": round(sell_price, 2) if sell_price is not None else None,
        "buy_5pct": round(buy_5pct, 2) if buy_5pct else None,
        "sell_5pct": round(sell_5pct, 2) if sell_5pct else None,
        "buy_orders": len(buy_orders),
        "sell_orders": len(sell_orders),
        "buy_threshold": round(buy_threshold, 2) if buy_threshold is not None else None,
        "sell_threshold": round(sell_threshold, 2) if sell_threshold is not None else None,
        "buy_volume": buy_volume,
        "sell_volume": sell_volume,
        "total_value": round(total_value, 2),
    })


@app.route("/v1/hub-compare/<type_id>", methods=["GET"])
def get_hub_compare(type_id: str):
    """Compare market stats across main hubs."""
    try:
        type_id_int = int(type_id)
    except ValueError:
        return json_error("UNKNOWN_ITEMS", "Invalid type ID format")

    if not is_known_type(type_id_int):
        return json_error("UNKNOWN_ITEMS", f"Unknown type ID: {type_id}")

    result = {}
    has_data = False

    for hub_key, hub_info in MAIN_HUBS.items():
        hub_system_id = hub_info["system_id"]

        # Find orders for this type at this hub's system
        hub_orders = []
        for market, type_orders in price_books.items():
            if type_id_int in type_orders:
                for order in type_orders[type_id_int]:
                    if order["system_id"] == hub_system_id:
                        hub_orders.append(order)

        if not hub_orders:
            continue

        has_data = True

        # Filter outliers
        filtered = filter_outliers(hub_orders)
        buy_orders = [o for o in filtered if o["is_buy_order"]]
        sell_orders = [o for o in filtered if not o["is_buy_order"]]

        # Calculate stats
        buy_price = max((o["price"] for o in buy_orders), default=None)
        sell_price = min((o["price"] for o in sell_orders), default=None)

        buy_volume = sum(o["volume_remain"] for o in buy_orders)
        sell_volume = sum(o["volume_remain"] for o in sell_orders)

        buy_5pct = calculate_weighted_avg_price(buy_orders, 0.05, buy=True)
        sell_5pct = calculate_weighted_avg_price(sell_orders, 0.05, buy=False)

        # Value = 5pct sell * sell volume in billions
        value = (sell_5pct * sell_volume) / 1e9 if sell_5pct else 0.0

        result[f"{hub_key}_sell"] = round(sell_price, 2) if sell_price is not None else None
        result[f"{hub_key}_buy"] = round(buy_price, 2) if buy_price is not None else None
        result[f"{hub_key}_sell_volume"] = float(sell_volume)
        result[f"{hub_key}_buy_volume"] = float(buy_volume)
        result[f"{hub_key}_value"] = round(value, 2)
        result[f"{hub_key}_sell_5pct"] = round(sell_5pct, 2) if sell_5pct else None
        result[f"{hub_key}_buy_5pct"] = round(buy_5pct, 2) if buy_5pct else None

    if not has_data:
        return json_error("NO_PRICE_DATA", "No hub data exists for this item")

    return json_ok(result)


def json_ok(data: dict, status: int = 200) -> tuple:
    """Return a successful JSON response."""
    return jsonify(data), status


def json_error(error: str, details: str) -> tuple:
    """Return an error JSON response."""
    return jsonify({"error": error, "details": details}), 404 if error == "UNKNOWN_ITEMS" else 400


def create_api_key() -> str:
    """Create a new API key."""
    return secrets.token_urlsafe(32)


def validate_config(config: dict) -> list[str]:
    """Validate configuration and return list of errors. Errors contain 'path' and 'message'."""
    errors = []

    # Validate structure
    if 'structure' in config:
        struct = config['structure']

        if struct.get('type') not in ['npc_station', 'citadel', 'athanor', 'tatara', None]:
            if struct.get('type') is not None:
                errors.append({
                    "code": "INVALID_VALUE",
                    "path": "/structure/type",
                    "message": "Invalid structure type"
                })

        if struct.get('rig') not in ['t1', 't2', None]:
            if struct.get('rig') is not None:
                errors.append({
                    "code": "INVALID_VALUE",
                    "path": "/structure/rig",
                    "message": "Invalid rig type"
                })

        if struct.get('security') not in ['highsec', 'lowsec', 'nullsec', None]:
            if struct.get('security') is not None:
                errors.append({
                    "code": "INVALID_VALUE",
                    "path": "/structure/security",
                    "message": "Invalid security status"
                })

        if struct.get('type') == 'npc_station' and struct.get('rig') not in [None, 'null']:
            errors.append({
                "code": "INVALID_VALUE",
                "path": "/structure/rig",
                "message": "npc_station cannot have a rig"
            })

    # Validate skills
    if 'skills' in config:
        for skill, level in config['skills'].items():
            if not isinstance(level, int) or level < 0 or level > 5:
                errors.append({
                    "code": "INVALID_VALUE",
                    "path": f"/skills/{skill}",
                    "message": "Skill level must be an integer 0-5"
                })

    # Validate implant
    if config.get('implant') not in [None, 'null', 'RX-801', 'RX-802', 'RX-804']:
        errors.append({
            "code": "INVALID_VALUE",
            "path": "/implant",
            "message": "Invalid implant type"
        })

    return errors


def reprocess_items(items: dict[str, int], efficiency: dict, drill_down: bool, api_key: str | None) -> dict:
    """Reprocess items and return results."""
    # Get yield values
    yields = calculate_yield(api_key, efficiency)

    # Track inputs and outputs
    input_volume = 0.0
    products = defaultdict(lambda: {"quantity": 0, "volume": 0.0, "buy": None, "sell": None})
    reprocessable_outputs = defaultdict(int)  # For drill-down

    # Process each item
    for item_name, quantity in items.items():
        if quantity <= 0:
            continue

        type_id = get_type_id(item_name)
        if type_id is None:
            return {"error": "UNKNOWN_ITEMS", "details": [item_name]}

        type_info = type_ids[type_id]
        item_volume = type_info['volume']
        portion_size = type_info['portionSize']

        # Calculate input volume
        input_volume += quantity * item_volume

        # Check if reprocessable
        if not is_reprocessable(type_id):
            # Not reprocessable, add to products unchanged
            type_name = get_type_name_by_id(type_id)
            if type_name:
                products[type_name]["quantity"] += quantity
                products[type_name]["volume"] += quantity * item_volume
            continue

        # Calculate portions
        portions = quantity // portion_size
        if portions == 0:
            # Less than one portion, can't reprocess
            type_name = get_type_name_by_id(type_id)
            if type_name:
                products[type_name]["quantity"] += quantity
                products[type_name]["volume"] += quantity * item_volume
            continue

        # Get material outputs for all portions
        material_totals = defaultdict(int)
        for material_id, material_qty in type_materials[type_id]:
            material_totals[material_id] += portions * material_qty

        # Get efficiency for this item type
        eff = get_efficiency_for_item(type_id, yields)

        # Apply efficiency and round down
        for mat_id, total_qty in material_totals.items():
            processed = int(total_qty * eff)
            if processed > 0:
                material_name = get_type_name_by_id(mat_id)
                if material_name:
                    products[material_name]["quantity"] += processed
                    reprocessable_outputs[mat_id] += processed

        # Add back leftover units (not full portions)
        leftovers = quantity % portion_size
        if leftovers > 0:
            type_name = get_type_name_by_id(type_id)
            if type_name:
                products[type_name]["quantity"] += leftovers
                products[type_name]["volume"] += leftovers * item_volume

    # Drill-down reprocessing
    if drill_down:
        products = drill_down_reprocess(products, reprocessable_outputs, yields, efficiency)

    # Calculate output volume
    output_volume = sum(p["volume"] for p in products.values())

    return {
        "input_volume": round(input_volume, 2),
        "output_volume": round(output_volume, 2),
        "products": dict(products),
        "yields": yields
    }


def drill_down_reprocess(products: dict, reprocessable_outputs: dict, yields: dict, efficiency: dict) -> dict:
    """Reprocess any reprocessable outputs."""
    # Keep reprocessing until nothing more can be broken down
    changed = True
    while changed:
        changed = False
        new_products = defaultdict(lambda: {"quantity": 0, "volume": 0.0, "buy": None, "sell": None})

        for item_name, product in products.items():
            type_id = get_type_id(item_name)
            if type_id is None:
                new_products[item_name] = product
                continue

            # Copy existing product data
            for key in product:
                new_products[item_name][key] = product[key]

            # Check if this item is reprocessable
            if not is_reprocessable(type_id):
                continue

            type_info = type_ids[type_id]
            portion_size = type_info['portionSize']

            # Check if we have enough to reprocess
            quantity = product["quantity"]
            if quantity < portion_size:
                continue

            # Remove from current products (will be replaced with materials)
            new_products[item_name]["quantity"] = quantity % portion_size
            new_products[item_name]["volume"] = (quantity % portion_size) * type_info['volume']

            # Process the portions
            portions = quantity // portion_size

            # Get material outputs for all portions
            material_totals = defaultdict(int)
            for material_id, material_qty in type_materials[type_id]:
                material_totals[material_id] += portions * material_qty

            # Get efficiency for this item type
            eff = get_efficiency_for_item(type_id, yields)

            # Apply efficiency and round down
            for mat_id, total_qty in material_totals.items():
                processed = int(total_qty * eff)
                if processed > 0:
                    material_name = get_type_name_by_id(mat_id)
                    if material_name:
                        new_products[material_name]["quantity"] += processed

                        # Update volume
                        if material_name in type_ids:
                            mat_volume = type_ids[mat_id]['volume']
                            new_products[material_name]["volume"] += processed * mat_volume

        # Check if products changed
        for name, product in products.items():
            if name in new_products:
                if product['quantity'] != new_products[name]['quantity']:
                    changed = True
                    break
        else:
            # Check for new products
            if set(products.keys()) != set(new_products.keys()):
                changed = True

        products = dict(new_products)

    return products


def get_market_prices(type_name: str, market: str | None) -> dict:
    """Get market prices for an item."""
    if not market or market not in price_books:
        return {"buy": None, "sell": None}

    type_id = get_type_id(type_name)
    if type_id is None:
        return {"buy": None, "sell": None}

    orders = price_books[market].get(type_id, [])
    if not orders:
        return {"buy": None, "sell": None}

    buy_orders = [o for o in orders if o['is_buy_order']]
    sell_orders = [o for o in orders if not o['is_buy_order']]

    result = {}

    if buy_orders:
        best_buy = max(buy_orders, key=lambda x: x['price'])
        result['buy'] = round(best_buy['price'], 2)
    else:
        result['buy'] = None

    if sell_orders:
        best_sell = min(sell_orders, key=lambda x: x['price'])
        result['sell'] = round(best_sell['price'], 2)
    else:
        result['sell'] = None

    return result


def enrich_products_with_prices(products: dict[str, dict], market: str | None) -> dict:
    """Add buy/sell prices to products."""
    if not market or market not in price_books:
        for name in products:
            products[name]["buy"] = None
            products[name]["sell"] = None
        return products

    for item_name, product in products.items():
        prices = get_market_prices(item_name, market)
        product["buy"] = prices["buy"]
        product["sell"] = prices["sell"]

    return products


def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Market Tools API")
    parser.add_argument("--port", type=int, default=5000, help="Port to listen on")
    parser.add_argument("--address", type=str, default="127.0.0.1", help="Address to bind to")
    parser.add_argument("--sde", type=str, required=True, help="Path to SDE directory")

    args = parser.parse_args()

    global SDE_PATH
    SDE_PATH = pathlib.Path(args.sde)

    if not SDE_PATH.exists():
        print(f"Error: SDE path does not exist: {SDE_PATH}")
        return 1

    print(f"Loading SDE from {SDE_PATH}...")
    load_sde(SDE_PATH)
    print(f"Loaded {len(type_names)} types, {len(region_names)} regions")

    app.run(host=args.address, port=args.port, debug=False)
    return 0


if __name__ == "__main__":
    exit(main())
