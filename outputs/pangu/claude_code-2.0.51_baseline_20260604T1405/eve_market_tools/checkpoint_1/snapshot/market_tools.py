#!/usr/bin/env python3
"""Market analysis API for industrialist market tooling."""

import gzip
import bz2
import csv
import io
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from flask import Flask, request, jsonify

app = Flask(__name__)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Constants
MAIN_HUBS = {
    "jita": 60003760,  # Jita IV - Moon 4 - Caldari Navy Assembly Plant
    "amarr": 60008494,  # Amarr VIII (Oris) - Emperor Family Academy
    "dodixie": 60011663,  # Dodixie IX - Moon 20 - Federation Navy Assembly Plant
    "rens": 60004440,  # Rens VI - Moon 8 - Brutor Tribe Treasury
    "hek": 60005612,  # Hek VIII - Moon 12 - Boundless Creation Factory
}

# Region ID to hub mapping (The Forge region contains Jita)
REGION_HUBS = {
    10000002: "jita",  # The Forge
    10000043: "amarr",  # Domain
    10000031: "dodixie",  # Sinq Laison
    10000030: "rens",  # Heimatar
    10000039: "hek",  # Metropolis
}


@dataclass
class SDE:
    """Static Data Export loader for EVE Online."""

    path: str
    type_names: dict[int, str] = field(default_factory=dict)
    type_ids: dict[str, int] = field(default_factory=dict)
    region_ids: dict[int, str] = field(default_factory=dict)
    region_names: dict[str, int] = field(default_factory=dict)
    station_names: dict[int, str] = field(default_factory=dict)
    station_ids: dict[str, int] = field(default_factory=dict)
    solar_systems: dict[int, str] = field(default_factory=dict)
    loaded: bool = False

    def load(self) -> None:
        """Load all SDE data from bz2 files."""
        if self.loaded:
            return

        # Load invTypes.csv for type name/ID mapping
        self._load_csv("invTypes.csv", lambda row: {
            "id": int(row["typeID"]),
            "name": row["typeName"]
        }, lambda item: self._add_type(item))

        # Load mapRegions.csv for region name/ID mapping
        self._load_csv("mapRegions.csv", lambda row: {
            "id": int(row["regionID"]),
            "name": row["regionName"]
        }, lambda item: self._add_region(item))

        # Load staStations.csv for station name/ID mapping
        self._load_csv("staStations.csv", lambda row: {
            "id": int(row["stationID"]),
            "name": row["stationName"]
        }, lambda item: self._add_station(item))

        self.loaded = True
        logger.info("SDE loaded successfully")

    def _load_csv(self, filename: str, parse_row, add_item) -> None:
        """Load a bz2 compressed CSV file."""
        filepath = f"{self.path}/{filename}.bz2"
        try:
            with bz2.open(filepath, "rt", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    try:
                        item = parse_row(row)
                        add_item(item)
                    except Exception as e:
                        logger.warning(f"Error parsing row in {filename}: {e}")
        except Exception as e:
            logger.error(f"Error loading {filename}: {e}")
            raise

    def _add_type(self, item) -> None:
        self.type_names[item["id"]] = item["name"]
        self.type_ids[item["name"]] = item["id"]

    def _add_region(self, item) -> None:
        self.region_ids[item["id"]] = item["name"]
        self.region_names[item["name"]] = item["id"]

    def _add_station(self, item) -> None:
        self.station_names[item["id"]] = item["name"]
        self.station_ids[item["name"]] = item["id"]

    def get_type_name(self, type_id: int) -> str:
        """Get type name by ID."""
        return self.type_names.get(type_id, "")

    def get_type_id(self, name: str) -> int | None:
        """Get type ID by name."""
        return self.type_ids.get(name)

    def get_region_name(self, region_id: int) -> str:
        """Get region name by ID."""
        return self.region_ids.get(region_id, "")

    def get_station_name(self, station_id: int) -> str:
        """Get station name by ID."""
        return self.station_names.get(station_id, "")


@dataclass
class Order:
    """Represents a market order."""
    order_id: int
    duration: int
    is_buy_order: bool
    issued: datetime
    location_id: int
    min_volume: int
    price: float
    range: str
    system_id: int
    type_id: int
    volume_remain: int
    volume_total: int


@dataclass
class Station:
    """Represents a station or structure."""
    location_id: int
    type: str  # "Structure" or "Station"
    name: str
    system: str = ""  # Extracted from name for structures


class MarketData:
    """In-memory market data storage."""

    def __init__(self):
        self.prices: dict[str, dict[int, list[Order]]] = defaultdict(lambda: defaultdict(list))
        self.stations: dict[int, Station] = {}
        self.sde: SDE | None = None

    def set_sde(self, sde: SDE) -> None:
        """Set the SDE reference."""
        self.sde = sde

    def ingest_prices(self, market: str, orders: list[Order],
                      mode: str = "replace", location_id: int | None = None) -> int:
        """Ingest price data for a market."""
        if location_id is not None:
            orders = [o for o in orders if o.location_id == location_id]

        if mode == "replace":
            self.prices[market].clear()

        count = 0
        for order in orders:
            existing = self.prices[market][order.type_id]

            if mode == "append":
                # Find existing order with same ID
                existing_idx = None
                for i, o in enumerate(existing):
                    if o.order_id == order.order_id:
                        existing_idx = i
                        break

                # Only replace if new order is newer, or if no existing order
                if existing_idx is not None:
                    if order.issued > existing[existing_idx].issued:
                        existing[existing_idx] = order
                        count += 1
                else:
                    existing.append(order)
                    count += 1
            else:
                existing.append(order)
                count += 1

        return count

    def ingest_stations(self, stations: list[Station]) -> int:
        """Ingest station data."""
        for station in stations:
            # Extract system from structure name if needed
            if station.type == "Structure":
                parts = station.name.split(" - ")
                if len(parts) >= 2:
                    station.system = parts[0]
            self.stations[station.location_id] = station
        return len(stations)

    def get_orders_for_market(self, market: str, type_id: int | None = None) -> list[Order]:
        """Get orders for a market, optionally filtered by type."""
        if type_id is not None:
            return list(self.prices[market][type_id])
        orders = []
        for type_orders in self.prices[market].values():
            orders.extend(type_orders)
        return orders

    def get_all_markets(self) -> list[str]:
        """Get all markets with data."""
        return list(self.prices.keys())


# Global data store
data = MarketData()


def parse_price_csv(content: bytes) -> list[Order]:
    """Parse gzipped CSV content into Order objects."""
    try:
        # Decompress gzip
        decompressed = gzip.decompress(content)
        text = decompressed.decode("utf-8")
    except gzip.BadGzipFile:
        # Try without decompression (might be plain text)
        text = content.decode("utf-8")
    except Exception as e:
        raise ValueError(f"Decompression failed: {str(e)}")

    orders = []
    try:
        reader = csv.DictReader(io.StringIO(text))
        required_columns = {
            "order_id", "duration", "is_buy_order", "issued",
            "location_id", "min_volume", "price", "range",
            "system_id", "type_id", "volume_remain", "volume_total"
        }

        if not reader.fieldnames:
            raise ValueError("Empty CSV file")

        actual_columns = set(reader.fieldnames)
        if not required_columns.issubset(actual_columns):
            missing = required_columns - actual_columns
            raise ValueError(f"Missing required columns: {missing}")

        for row_num, row in enumerate(reader, start=2):  # Start at 2 (header is row 1)
            try:
                order = Order(
                    order_id=int(row["order_id"]),
                    duration=int(row["duration"]),
                    is_buy_order=row["is_buy_order"].lower() == "true",
                    issued=datetime.fromisoformat(row["issued"].replace("Z", "+00:00")),
                    location_id=int(row["location_id"]),
                    min_volume=int(row["min_volume"]),
                    price=float(row["price"]),
                    range=row["range"],
                    system_id=int(row["system_id"]),
                    type_id=int(row["type_id"]),
                    volume_remain=int(row["volume_remain"]),
                    volume_total=int(row["volume_total"])
                )
                orders.append(order)
            except Exception as e:
                raise ValueError(f"Error parsing row {row_num}: {str(e)}")
    except Exception as e:
        raise ValueError(f"CSV parsing failed: {str(e)}")

    return orders


def parse_station_csv(content: bytes) -> list[Station]:
    """Parse gzipped CSV content into Station objects."""
    try:
        decompressed = gzip.decompress(content)
        text = decompressed.decode("utf-8")
    except gzip.BadGzipFile:
        text = content.decode("utf-8")
    except Exception as e:
        raise ValueError(f"Decompression failed: {str(e)}")

    stations = []
    try:
        reader = csv.DictReader(io.StringIO(text))
        required_columns = {"location_id", "type", "name"}

        if not reader.fieldnames:
            raise ValueError("Empty CSV file")

        actual_columns = set(reader.fieldnames)
        if not required_columns.issubset(actual_columns):
            missing = required_columns - actual_columns
            raise ValueError(f"Missing required columns: {missing}")

        for row_num, row in enumerate(reader, start=2):
            try:
                station = Station(
                    location_id=int(row["location_id"]),
                    type=row["type"],
                    name=row["name"]
                )
                stations.append(station)
            except Exception as e:
                raise ValueError(f"Error parsing row {row_num}: {str(e)}")
    except Exception as e:
        raise ValueError(f"CSV parsing failed: {str(e)}")

    return stations


def filter_outlier_orders(orders: list[Order], is_buy: bool) -> list[Order]:
    """Filter out outliers: 10% of highest buy or 10x lowest sell."""
    if not orders:
        return orders

    if is_buy:
        # Buy orders: filter out 10% of highest prices
        sorted_orders = sorted(orders, key=lambda o: o.price, reverse=True)
        num_orders = len(sorted_orders)
        # Calculate number to remove: 10% rounded up (at least 1)
        to_remove = max(1, (num_orders + 9) // 10)  # Ceiling division by 10
        # Keep all if removing all or none to remove
        if to_remove >= num_orders or to_remove == 0:
            return orders
        # Threshold is the price of the last order we're keeping
        # After removing 'to_remove' highest, we keep from index 'to_remove' onwards
        # So threshold is the minimum price among kept orders (at index to_remove)
        keep_start_idx = to_remove
        threshold = sorted_orders[keep_start_idx].price
        return [o for o in orders if o.price <= threshold]
    else:
        # Sell orders: filter out orders >= 10x the lowest sell
        sorted_orders = sorted(orders, key=lambda o: o.price)
        if not sorted_orders:
            return orders
        lowest = sorted_orders[0].price
        threshold = lowest * 10
        return [o for o in orders if o.price < threshold]


def calculate_volume_weighted_price(orders: list[Order], is_buy: bool, percent: float = 0.05) -> float:
    """Calculate volume-weighted average price for top X% of orders by price."""
    if not orders:
        return 0.0

    # Sort by price (descending for buy, ascending for sell)
    sorted_orders = sorted(orders, key=lambda o: o.price, reverse=is_buy)

    total_volume = sum(o.volume_remain for o in orders)
    target_volume = total_volume * percent

    if total_volume == 0:
        return 0.0

    accumulated = 0
    weighted_sum = 0.0

    for order in sorted_orders:
        if accumulated >= target_volume:
            break

        remaining_needed = target_volume - accumulated
        take = min(order.volume_remain, remaining_needed)
        weighted_sum += take * order.price
        accumulated += take

    if accumulated == 0:
        return 0.0

    return weighted_sum / accumulated


@app.route("/v1/prices", methods=["POST"])
def ingest_prices():
    """Ingest price data from gzipped CSV."""
    if not request.data:
        return jsonify({
            "error": "INVALID_FORMAT",
            "details": "No data provided"
        }), 400

    market = request.args.get("market", "jita")
    mode = request.args.get("mode", "replace")
    location_id_str = request.args.get("location_id")
    location_id = int(location_id_str) if location_id_str else None

    try:
        orders = parse_price_csv(request.data)
    except ValueError as e:
        return jsonify({
            "error": "INVALID_FORMAT",
            "details": str(e)
        }), 400

    count = data.ingest_prices(market, orders, mode, location_id)

    return jsonify({
        "status": "PRICES_UPDATED",
        "count": count
    }), 200


@app.route("/v1/stations", methods=["POST"])
def ingest_stations():
    """Ingest station data from gzipped CSV."""
    if not request.data:
        return jsonify({
            "error": "INVALID_FORMAT",
            "details": "No data provided"
        }), 400

    try:
        stations = parse_station_csv(request.data)
    except ValueError as e:
        return jsonify({
            "error": "INVALID_FORMAT",
            "details": str(e)
        }), 400

    count = data.ingest_stations(stations)

    return jsonify({
        "status": "STATIONS_UPDATED",
        "count": count
    }), 200


@app.route("/v1/market/<int:region_id>", methods=["GET"])
def get_market(region_id: int):
    """Get market statistics for a region."""
    # Determine hub for region
    hub = REGION_HUBS.get(region_id)
    if not hub:
        return jsonify({
            "error": "UNKNOWN_ITEMS",
            "details": f"Region {region_id} not recognized or no hub configured"
        }), 404

    market_name = data.sde.get_region_name(region_id) if data.sde else ""
    if not market_name:
        market_name = f"Region {region_id}"

    type_ids_param = request.args.get("type_ids")
    include_hubs = "hubs" in request.args

    # Get orders for this hub's market
    orders = data.get_orders_for_market(hub)

    # Determine the regional hub (location with most sell orders)
    sell_orders = [o for o in orders if not o.is_buy_order]
    hub_by_location = defaultdict(int)
    for o in sell_orders:
        hub_by_location[o.location_id] += 1

    if not sell_orders:
        # No sell orders, return empty stats
        result = {
            "name": market_name,
            "sell_orders": 0,
            "buy_orders": 0,
            "sell_value": 0.0,
            "buy_value": 0.0,
            "unique_items": 0
        }
        if include_hubs:
            result["hubs"] = []
        return jsonify(result), 200

    # Find hub with most sell orders
    regional_hub_loc = max(hub_by_location.items(), key=lambda x: x[1])[0]

    # Filter orders to regional hub
    hub_orders = [o for o in orders if o.location_id == regional_hub_loc]

    # Get stats for regional hub
    hub_sell_orders = [o for o in hub_orders if not o.is_buy_order]
    hub_buy_orders = [o for o in hub_orders if o.is_buy_order]

    # Filter outliers
    filtered_sell = filter_outlier_orders(hub_sell_orders, False)
    filtered_buy = filter_outlier_orders(hub_buy_orders, True)

    # Calculate values
    sell_value = sum(o.price * o.volume_remain for o in filtered_sell) / 1e9
    buy_value = sum(o.price * o.volume_remain for o in filtered_buy) / 1e9

    # Count unique items
    unique_items = len(set(o.type_id for o in hub_orders))

    result = {
        "name": market_name,
        "sell_orders": len(hub_sell_orders),
        "buy_orders": len(hub_buy_orders),
        "sell_value": round(sell_value, 2),
        "buy_value": round(buy_value, 2),
        "unique_items": unique_items
    }

    # If type_ids specified, get per-type stats
    if type_ids_param:
        type_ids = [int(tid.strip()) for tid in type_ids_param.split(",")]
        result["types"] = {}

        for tid in type_ids:
            type_orders = [o for o in hub_orders if o.type_id == tid]
            type_sell = [o for o in type_orders if not o.is_buy_order]
            type_buy = [o for o in type_orders if o.is_buy_order]

            filtered_type_sell = filter_outlier_orders(type_sell, False)
            filtered_type_buy = filter_outlier_orders(type_buy, True)

            type_name = data.sde.get_type_name(tid) if data.sde else str(tid)

            buy_price = max((o.price for o in filtered_type_buy), default=None) if filtered_type_buy else None
            sell_price = min((o.price for o in filtered_type_sell), default=None) if filtered_type_sell else None

            split_price = None
            if buy_price is not None and sell_price is not None:
                split_price = (buy_price + sell_price) / 2
            elif buy_price is not None:
                split_price = buy_price
            elif sell_price is not None:
                split_price = sell_price

            result["types"][type_name] = {
                "buy": round(buy_price, 2) if buy_price is not None else None,
                "sell": round(sell_price, 2) if sell_price is not None else None,
                "split": round(split_price, 2) if split_price is not None else None,
                "buy_orders": len(type_buy),
                "sell_orders": len(type_sell)
            }

    # Include hub statistics
    if include_hubs:
        result["hubs"] = []
        hub_locations = defaultdict(list)
        for o in hub_sell_orders:
            hub_locations[o.location_id].append(o)

        for loc_id, loc_orders in hub_locations.items():
            loc_value = sum(o.price * o.volume_remain for o in loc_orders) / 1e9
            station_name = data.sde.get_station_name(loc_id) if data.sde else None
            if station_name:
                result["hubs"].append({
                    "station": station_name,
                    "orders": len(loc_orders),
                    "sell_value": round(loc_value, 2)
                })
            else:
                result["hubs"].append({
                    "station": str(loc_id),
                    "orders": len(loc_orders),
                    "sell_value": round(loc_value, 2)
                })

        # Sort by number of orders descending
        result["hubs"].sort(key=lambda x: x["orders"], reverse=True)

    return jsonify(result), 200


@app.route("/v1/market/<int:region_id>/<int:type_id>", methods=["GET"])
def get_market_type(region_id: int, type_id: int):
    """Get detailed market statistics for a specific type in a region."""
    # Determine hub for region
    hub = REGION_HUBS.get(region_id)
    if not hub:
        return jsonify({
            "error": "UNKNOWN_ITEMS",
            "details": f"Region {region_id} not recognized or no hub configured"
        }), 404

    type_name = data.sde.get_type_name(type_id) if data.sde else ""

    # Get orders for this hub's market
    all_orders = data.get_orders_for_market(hub, type_id)

    if not all_orders:
        return jsonify({
            "error": "UNKNOWN_ITEMS",
            "details": f"No orders found for type {type_id} in region {region_id}"
        }), 404

    sell_orders = [o for o in all_orders if not o.is_buy_order]
    buy_orders = [o for o in all_orders if o.is_buy_order]

    # Filter outliers
    filtered_sell = filter_outlier_orders(sell_orders, False)
    filtered_buy = filter_outlier_orders(buy_orders, True)

    # Calculate stats
    best_buy = max((o.price for o in filtered_buy), default=None) if filtered_buy else None
    lowest_sell = min((o.price for o in filtered_sell), default=None) if filtered_sell else None

    buy_5pct = calculate_volume_weighted_price(filtered_buy, True)
    sell_5pct = calculate_volume_weighted_price(filtered_sell, False)

    # Calculate thresholds
    buy_threshold = best_buy * 0.9 if best_buy else 0.0
    sell_threshold = lowest_sell * 1.1 if lowest_sell else 0.0

    # Calculate volumes
    buy_volume = sum(o.volume_remain for o in filtered_buy)
    sell_volume = sum(o.volume_remain for o in filtered_sell)

    # Calculate total value (sell 5pct * sell volume)
    total_value = (sell_5pct * sell_volume) / 1e9 if sell_5pct and sell_volume > 0 else 0.0

    result = {
        "name": type_name if type_name else str(type_id),
        "buy": round(best_buy, 2) if best_buy is not None else None,
        "sell": round(lowest_sell, 2) if lowest_sell is not None else None,
        "buy_5pct": round(buy_5pct, 2),
        "sell_5pct": round(sell_5pct, 2),
        "buy_orders": len(buy_orders),
        "sell_orders": len(sell_orders),
        "buy_threshold": round(buy_threshold, 2),
        "sell_threshold": round(sell_threshold, 2),
        "buy_volume": buy_volume,
        "sell_volume": sell_volume,
        "total_value": round(total_value, 2)
    }

    return jsonify(result), 200


@app.route("/v1/hub-compare/<int:type_id>", methods=["GET"])
def hub_compare(type_id: int):
    """Compare market statistics across main hubs."""
    result = {}
    has_data = False

    for hub_name, station_id in MAIN_HUBS.items():
        # Get orders for this hub's market
        orders = data.get_orders_for_market(hub_name, type_id)

        if not orders:
            continue

        sell_orders = [o for o in orders if not o.is_buy_order]
        buy_orders = [o for o in orders if o.is_buy_order]

        # Filter outliers
        filtered_sell = filter_outlier_orders(sell_orders, False)
        filtered_buy = filter_outlier_orders(buy_orders, True)

        best_buy = max((o.price for o in filtered_buy), default=None) if filtered_buy else None
        lowest_sell = min((o.price for o in filtered_sell), default=None) if filtered_sell else None

        sell_5pct = calculate_volume_weighted_price(filtered_sell, False)
        buy_5pct = calculate_volume_weighted_price(filtered_buy, True)

        sell_volume = sum(o.volume_remain for o in filtered_sell)
        buy_volume = sum(o.volume_remain for o in filtered_buy)

        # Value = 5pct sell * sell volume, in billions
        value = (sell_5pct * sell_volume) / 1e9 if sell_5pct and sell_volume > 0 else 0.0

        result[f"{hub_name}_sell"] = round(lowest_sell, 2) if lowest_sell is not None else None
        result[f"{hub_name}_buy"] = round(best_buy, 2) if best_buy is not None else None
        result[f"{hub_name}_sell_volume"] = sell_volume
        result[f"{hub_name}_buy_volume"] = buy_volume
        result[f"{hub_name}_value"] = round(value, 2)
        result[f"{hub_name}_sell_5pct"] = round(sell_5pct, 2)
        result[f"{hub_name}_buy_5pct"] = round(buy_5pct, 2)
        has_data = True

    if not has_data:
        return jsonify({
            "error": "NO_PRICE_DATA",
            "details": f"No price data found for type {type_id} in any hub"
        }), 404

    return jsonify(result), 200


def main():
    """Main entry point for CLI."""
    import argparse

    parser = argparse.ArgumentParser(description="Market analysis API")
    parser.add_argument("--port", type=int, default=5000, help="Port to run on")
    parser.add_argument("--address", type=str, default="127.0.0.1", help="Address to bind to")
    parser.add_argument("--sde", type=str, required=True, help="Path to SDE directory")

    args = parser.parse_args()

    # Initialize SDE
    sde = SDE(args.sde)
    sde.load()
    data.set_sde(sde)

    # Start Flask app
    app.run(host=args.address, port=args.port, debug=False)


if __name__ == "__main__":
    main()
