#!/usr/bin/env python3
"""Market Tools API for industrial market analysis."""

import asyncio
import csv
import gzip
import io
import json
import pathlib
from datetime import datetime
from typing import Any

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
    """Load SDE reference data."""
    global type_names, region_names, region_hubs

    # Load item type names
    types_file = sde_path / "invTypes.csv.bz2"
    if types_file.exists():
        import bz2
        with bz2.open(types_file, "rt") as f:
            reader = csv.DictReader(f)
            for row in reader:
                type_id = int(row["typeID"])
                type_name = row["typeName"]
                type_names[type_id] = type_name

    # Load regions
    regions_file = sde_path / "mapRegions.csv.bz2"
    if regions_file.exists():
        import bz2
        with bz2.open(regions_file, "rt") as f:
            reader = csv.DictReader(f)
            for row in reader:
                region_id = int(row["regionID"])
                region_name = row["regionName"]
                region_names[region_id] = region_name

    # Load solar systems to build region->system mapping
    systems_file = sde_path / "mapSolarSystems.csv.bz2"
    region_systems: dict[int, list[int]] = {}
    if systems_file.exists():
        import bz2
        with bz2.open(systems_file, "rt") as f:
            reader = csv.DictReader(f)
            for row in reader:
                system_id = int(row["solarSystemID"])
                region_id = int(row["regionID"])
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


def json_ok(data: dict) -> tuple:
    """Return a successful JSON response."""
    return jsonify(data), 200


def json_error(error: str, details: str) -> tuple:
    """Return an error JSON response."""
    return jsonify({"error": error, "details": details}), 404 if error == "UNKNOWN_ITEMS" else 400


from flask import jsonify


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
