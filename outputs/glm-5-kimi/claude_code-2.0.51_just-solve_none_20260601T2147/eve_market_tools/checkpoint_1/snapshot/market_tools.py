#!/usr/bin/env python3
"""
Market Tools API - Deep market tooling for industrialists.
"""

import argparse
import csv
import gzip
import io
import math
import os
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

import bz2
import yaml
from fastapi import FastAPI, Header, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

app = FastAPI()

# Global state
sde_data: Dict[str, Any] = {}
market_data: Dict[str, Dict[int, List[Dict]]] = {}  # market_name -> type_id -> list of orders
station_data: Dict[int, Dict] = {}  # location_id -> station info
all_orders: List[Dict] = []  # All orders for region queries

# Hub definitions
HUB_STATIONS = {
    "jita": {
        "station_id": 60003760,
        "station_name": "Jita IV - Moon 4 - Caldari Navy Assembly Plant",
        "system_id": 30000142,
        "region_id": 10000002,
    },
    "amarr": {
        "station_id": 60008494,
        "station_name": "Amarr VIII (Oris) - Emperor Family Academy",
        "system_id": 30002187,
        "region_id": 10000043,
    },
    "dodixie": {
        "station_id": 60011866,
        "station_name": "Dodixie IX - Moon 20 - Federation Navy Assembly Plant",
        "system_id": 30002659,
        "region_id": 10000032,
    },
    "rens": {
        "station_id": 60004588,
        "station_name": "Rens VI - Moon 8 - Brutor Tribe Treasury",
        "system_id": 30002510,
        "region_id": 10000030,
    },
    "hek": {
        "station_id": 60005686,
        "station_name": "Hek VIII - Moon 12 - Boundless Creation Factory",
        "system_id": 30002053,
        "region_id": 10000042,
    },
}

# Required columns for prices CSV
PRICES_COLUMNS = {
    "order_id", "duration", "is_buy_order", "issued", "location_id",
    "min_volume", "price", "range", "system_id", "type_id",
    "volume_remain", "volume_total"
}

# Required columns for stations CSV
STATIONS_COLUMNS = {"location_id", "type", "name"}


def load_sde(sde_dir: str) -> None:
    """Load all SDE data from the specified directory."""
    global sde_data

    sde_data = {
        "types": {},  # type_id -> type_name
        "regions": {},  # region_id -> region_name
        "systems": {},  # system_id -> {region_id, name}
        "stations": {},  # station_id -> {system_id, region_id, name}
        "type_attributes": {},  # type_id -> {attr_id: value}
    }

    # Load invTypes
    type_path = os.path.join(sde_dir, "invTypes.csv.bz2")
    if os.path.exists(type_path):
        with bz2.open(type_path, "rt") as f:
            reader = csv.DictReader(f)
            for row in reader:
                type_id = int(row["typeID"])
                sde_data["types"][type_id] = {
                    "name": row["typeName"] or "",
                    "group_id": int(row["groupID"]) if row["groupID"] else None,
                    "volume": float(row["volume"]) if row["volume"] else 0.0,
                }

    # Load mapRegions
    region_path = os.path.join(sde_dir, "mapRegions.csv.bz2")
    if os.path.exists(region_path):
        with bz2.open(region_path, "rt") as f:
            reader = csv.DictReader(f)
            for row in reader:
                region_id = int(row["regionID"])
                sde_data["regions"][region_id] = row["regionName"]

    # Load mapSolarSystems
    system_path = os.path.join(sde_dir, "mapSolarSystems.csv.bz2")
    if os.path.exists(system_path):
        with bz2.open(system_path, "rt") as f:
            reader = csv.DictReader(f)
            for row in reader:
                system_id = int(row["solarSystemID"])
                sde_data["systems"][system_id] = {
                    "region_id": int(row["regionID"]),
                    "name": row["solarSystemName"],
                }

    # Load staStations
    station_path = os.path.join(sde_dir, "staStations.csv.bz2")
    if os.path.exists(station_path):
        with bz2.open(station_path, "rt") as f:
            reader = csv.DictReader(f)
            for row in reader:
                station_id = int(row["stationID"])
                sde_data["stations"][station_id] = {
                    "system_id": int(row["solarSystemID"]),
                    "region_id": int(row["regionID"]),
                    "name": row["stationName"],
                }


def get_type_name(type_id: int) -> Optional[str]:
    """Get type name from SDE data."""
    if type_id in sde_data["types"]:
        return sde_data["types"][type_id]["name"]
    return None


def get_region_name(region_id: int) -> Optional[str]:
    """Get region name from SDE data."""
    return sde_data["regions"].get(region_id)


def get_system_region(system_id: int) -> Optional[int]:
    """Get region ID for a system."""
    if system_id in sde_data["systems"]:
        return sde_data["systems"][system_id]["region_id"]
    return None


def get_station_info(location_id: int) -> Optional[Dict]:
    """Get station info from SDE or ingested station data."""
    # First check ingested station data
    if location_id in station_data:
        return station_data[location_id]
    # Then check SDE data
    if location_id in sde_data["stations"]:
        return sde_data["stations"][location_id]
    return None


def get_location_name(location_id: int) -> str:
    """Get location name from SDE or ingested station data."""
    info = get_station_info(location_id)
    if info:
        return info.get("name", str(location_id))
    return str(location_id)


def get_location_region(location_id: int) -> Optional[int]:
    """Get region ID for a location."""
    # Check SDE data first (authoritative for known stations)
    if location_id in sde_data["stations"]:
        return sde_data["stations"][location_id]["region_id"]
    # Then check ingested station data (for structures and custom stations)
    if location_id in station_data:
        station = station_data[location_id]
        if station.get("system_id"):
            return get_system_region(station["system_id"])
    return None


def parse_iso_datetime(dt_str: str) -> datetime:
    """Parse ISO-8601 datetime string."""
    # Handle various ISO-8601 formats
    dt_str = dt_str.replace("Z", "+00:00")
    if "+" not in dt_str and "-" not in dt_str[10:]:
        dt_str += "+00:00"
    try:
        return datetime.fromisoformat(dt_str)
    except ValueError:
        # Fallback for edge cases
        return datetime.strptime(dt_str.split("+")[0].split(".")[0], "%Y-%m-%dT%H:%M:%S")


def decompress_and_parse_csv(data: bytes) -> Tuple[List[Dict[str, Any]], Set[str]]:
    """Decompress gzipped data and parse CSV, returning rows and columns."""
    try:
        decompressed = gzip.decompress(data)
    except Exception as e:
        raise ValueError(f"Failed to decompress gzip data: {e}")

    try:
        text = decompressed.decode("utf-8")
    except UnicodeDecodeError:
        text = decompressed.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ValueError("CSV file has no headers")

    columns = set(reader.fieldnames)
    rows = list(reader)
    return rows, columns


def filter_outlier_orders(orders: List[Dict]) -> List[Dict]:
    """
    Filter out outlier orders per type:
    - Buy orders with price <= 10% of the highest buy price for that type
    - Sell orders with price >= 10x of the lowest sell price for that type
    """
    if not orders:
        return orders

    # Group by type_id
    by_type: Dict[int, List[Dict]] = defaultdict(list)
    for o in orders:
        by_type[o["type_id"]].append(o)

    filtered = []
    for type_id, type_orders in by_type.items():
        buy_orders = [o for o in type_orders if o.get("is_buy_order", False)]
        sell_orders = [o for o in type_orders if not o.get("is_buy_order", False)]

        if buy_orders:
            max_buy = max(o["price"] for o in buy_orders)
            threshold = max_buy * 0.10
            filtered.extend(o for o in buy_orders if o["price"] > threshold)

        if sell_orders:
            min_sell = min(o["price"] for o in sell_orders)
            threshold = min_sell * 10
            filtered.extend(o for o in sell_orders if o["price"] < threshold)

    return filtered


def calculate_5pct_price(orders: List[Dict], is_buy: bool) -> Optional[float]:
    """
    Calculate volume-weighted average price of top 5% of orders by best price.
    For buy orders, best price is highest. For sell orders, best price is lowest.
    """
    if not orders:
        return None

    # Sort by best price
    if is_buy:
        # Buy orders: highest price first
        sorted_orders = sorted(orders, key=lambda o: o["price"], reverse=True)
    else:
        # Sell orders: lowest price first
        sorted_orders = sorted(orders, key=lambda o: o["price"])

    total_volume = sum(o["volume_remain"] for o in sorted_orders)
    target_volume = total_volume * 0.05

    if target_volume == 0:
        return sorted_orders[0]["price"] if sorted_orders else None

    accumulated = 0.0
    total_value = 0.0

    for order in sorted_orders:
        vol = order["volume_remain"]
        price = order["price"]

        if accumulated + vol >= target_volume:
            # Take only what we need
            needed = target_volume - accumulated
            total_value += needed * price
            accumulated = target_volume
            break
        else:
            total_value += vol * price
            accumulated += vol

    if accumulated == 0:
        return None
    return total_value / accumulated


def get_regional_hub(orders: List[Dict], region_id: int) -> Optional[int]:
    """
    Find the regional hub (location with most sell orders) for a region.
    Returns location_id of the hub.
    """
    # Filter orders for this region
    region_orders = []
    for order in orders:
        location_id = order.get("location_id")
        order_region = get_location_region(location_id)
        if order_region == region_id:
            region_orders.append(order)

    if not region_orders:
        return None

    # Count sell orders by location
    sell_by_location: Dict[int, int] = defaultdict(int)
    for order in region_orders:
        if not order.get("is_buy_order", False):
            sell_by_location[order["location_id"]] += 1

    if not sell_by_location:
        return None

    # Find location with most sell orders
    return max(sell_by_location.keys(), key=lambda loc: sell_by_location[loc])


# API Endpoints

@app.post("/v1/prices")
async def ingest_prices(request: Request):
    """Ingest gzipped CSV bytes and build price books."""
    global market_data, all_orders

    # Get query parameters
    market = request.query_params.get("market", "jita").lower()
    mode = request.query_params.get("mode", "replace").lower()
    location_filter = request.query_params.get("location_id")

    # Get raw body
    try:
        data = await request.body()
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={"error": "INVALID_FORMAT", "details": f"Failed to read request body: {e}"}
        )

    # Decompress and parse CSV
    try:
        rows, columns = decompress_and_parse_csv(data)
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={"error": "INVALID_FORMAT", "details": str(e)}
        )

    # Validate required columns
    missing = PRICES_COLUMNS - columns
    if missing:
        return JSONResponse(
            status_code=400,
            content={"error": "INVALID_FORMAT", "details": f"Missing required columns: {missing}"}
        )

    # Parse and validate rows
    parsed_rows = []
    for row in rows:
        try:
            parsed = {
                "order_id": int(row["order_id"]),
                "duration": int(row["duration"]),
                "is_buy_order": row["is_buy_order"].lower() in ("true", "1", "yes"),
                "issued": row["issued"],
                "issued_dt": parse_iso_datetime(row["issued"]),
                "location_id": int(row["location_id"]),
                "min_volume": int(row["min_volume"]),
                "price": float(row["price"]),
                "range": row["range"],
                "system_id": int(row["system_id"]),
                "type_id": int(row["type_id"]),
                "volume_remain": int(row["volume_remain"]),
                "volume_total": int(row["volume_total"]),
            }
            parsed_rows.append(parsed)
        except (ValueError, KeyError) as e:
            return JSONResponse(
                status_code=400,
                content={"error": "INVALID_FORMAT", "details": f"Invalid row data: {e}"}
            )

    # Apply location filter if specified
    if location_filter:
        location_id = int(location_filter)
        parsed_rows = [r for r in parsed_rows if r["location_id"] == location_id]

    # Initialize market if needed
    if market not in market_data or mode == "replace":
        market_data[market] = defaultdict(list)

    # Process orders (upsert by order_id)
    order_index: Dict[int, Dict] = {}
    if mode == "append" and market in market_data:
        # Build index of existing orders
        for type_id, orders in market_data[market].items():
            for order in orders:
                order_index[order["order_id"]] = order

    # Update with new orders
    for row in parsed_rows:
        order_id = row["order_id"]
        if order_id in order_index:
            existing = order_index[order_id]
            # Newer issued wins
            if row["issued_dt"] > existing["issued_dt"]:
                order_index[order_id] = row
        else:
            order_index[order_id] = row

    # Rebuild market data from order index
    new_market_data: Dict[int, List[Dict]] = defaultdict(list)
    for order in order_index.values():
        new_market_data[order["type_id"]].append(order)

    market_data[market] = new_market_data

    # Update all_orders for region queries
    # Remove all orders for this market from all_orders
    all_orders[:] = [o for o in all_orders if o.get("_market") != market]

    # Add all orders from order_index (the upserted result)
    for order in order_index.values():
        row_copy = dict(order)
        row_copy["_market"] = market
        all_orders.append(row_copy)

    return JSONResponse(
        status_code=200,
        content={"status": "PRICES_UPDATED", "count": len(parsed_rows)}
    )


@app.post("/v1/stations")
async def ingest_stations(request: Request):
    """Ingest gzipped CSV bytes with station data."""
    global station_data

    # Get raw body
    try:
        data = await request.body()
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={"error": "INVALID_FORMAT", "details": f"Failed to read request body: {e}"}
        )

    # Decompress and parse CSV
    try:
        rows, columns = decompress_and_parse_csv(data)
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={"error": "INVALID_FORMAT", "details": str(e)}
        )

    # Validate required columns
    missing = STATIONS_COLUMNS - columns
    if missing:
        return JSONResponse(
            status_code=400,
            content={"error": "INVALID_FORMAT", "details": f"Missing required columns: {missing}"}
        )

    # Parse and store station data
    count = 0
    for row in rows:
        try:
            location_id = int(row["location_id"])
            station_type = row["type"]
            name = row["name"]

            # For structures, extract system from name
            # Structures have system name at beginning: "System Name - ..."
            system_id = None
            if station_type == "Structure":
                # Try to find system from name prefix
                for sys_id, sys_info in sde_data["systems"].items():
                    if name.startswith(sys_info["name"]):
                        system_id = sys_id
                        break

            station_data[location_id] = {
                "name": name,
                "type": station_type,
                "system_id": system_id,
            }
            count += 1
        except (ValueError, KeyError) as e:
            return JSONResponse(
                status_code=400,
                content={"error": "INVALID_FORMAT", "details": f"Invalid row data: {e}"}
            )

    return JSONResponse(
        status_code=200,
        content={"status": "STATIONS_UPDATED", "count": count}
    )


@app.get("/v1/market/{region_id}")
async def get_market_region(
    region_id: int = Path(...),
    type_ids: Optional[str] = Query(None),
    hubs: Optional[int] = Query(None),
):
    """Get market stats for a region."""
    global all_orders

    # Get region name
    region_name = get_region_name(region_id)
    if region_name is None:
        return JSONResponse(
            status_code=404,
            content={"error": "UNKNOWN_ITEMS", "details": f"Unknown region ID: {region_id}"}
        )

    # Filter orders for this region
    region_orders = []
    for order in all_orders:
        location_id = order.get("location_id")
        order_region = get_location_region(location_id)
        if order_region == region_id:
            region_orders.append(order)

    # Find regional hub
    hub_location_id = get_regional_hub(region_orders, region_id)

    # Calculate overall stats (filtering outliers)
    filtered_orders = filter_outlier_orders(region_orders)

    buy_orders = [o for o in filtered_orders if o.get("is_buy_order", False)]
    sell_orders = [o for o in filtered_orders if not o.get("is_buy_order", False)]

    buy_value = sum(o["price"] * o["volume_remain"] for o in buy_orders)
    sell_value = sum(o["price"] * o["volume_remain"] for o in sell_orders)

    unique_items = set()
    for o in filtered_orders:
        unique_items.add(o["type_id"])

    response: Dict[str, Any] = {
        "name": region_name,
        "sell_orders": len(sell_orders),
        "buy_orders": len(buy_orders),
        "sell_value": round(sell_value / 1e9, 2),  # Billions, rounded to 2 decimal points
        "buy_value": round(buy_value / 1e9, 2),
        "unique_items": len(unique_items),
    }

    # Handle type_ids parameter
    if type_ids:
        type_id_list = [int(tid.strip()) for tid in type_ids.split(",") if tid.strip()]
        types_response = {}

        for type_id in type_id_list:
            type_name = get_type_name(type_id)
            if type_name is None:
                return JSONResponse(
                    status_code=404,
                    content={"error": "UNKNOWN_ITEMS", "details": f"Unknown type ID: {type_id}"}
                )

            # Filter orders for this type at the hub
            hub_orders = [o for o in region_orders if o["type_id"] == type_id and o["location_id"] == hub_location_id]

            if not hub_orders:
                types_response[type_name] = {
                    "buy": None,
                    "sell": None,
                    "split": None,
                    "buy_orders": 0,
                    "sell_orders": 0,
                }
                continue

            type_buy = [o for o in hub_orders if o.get("is_buy_order", False)]
            type_sell = [o for o in hub_orders if not o.get("is_buy_order", False)]

            highest_buy = max((o["price"] for o in type_buy), default=None)
            lowest_sell = min((o["price"] for o in type_sell), default=None)

            split = None
            if highest_buy is not None and lowest_sell is not None:
                split = (highest_buy + lowest_sell) / 2

            types_response[type_name] = {
                "buy": highest_buy,
                "sell": lowest_sell,
                "split": split,
                "buy_orders": len(type_buy),
                "sell_orders": len(type_sell),
            }

        response["types"] = types_response

    # Handle hubs parameter
    if hubs is not None:
        hubs_list = []

        # Group orders by location
        orders_by_location: Dict[int, List[Dict]] = defaultdict(list)
        for order in region_orders:
            if not order.get("is_buy_order", False):  # Sell orders only
                orders_by_location[order["location_id"]].append(order)

        # Sort by number of sell orders
        sorted_locations = sorted(
            orders_by_location.keys(),
            key=lambda loc: len(orders_by_location[loc]),
            reverse=True
        )

        for i, loc_id in enumerate(sorted_locations[:hubs]):
            loc_orders = orders_by_location[loc_id]
            loc_sell_value = sum(o["price"] * o["volume_remain"] for o in loc_orders)

            # Get station name
            station_name = get_location_name(loc_id)

            hubs_list.append({
                "station": station_name,
                "orders": len(loc_orders),
                "sell_value": round(loc_sell_value / 1e9, 2),
            })

        response["hubs"] = hubs_list

    return JSONResponse(status_code=200, content=response)


@app.get("/v1/market/{region_id}/{type_id}")
async def get_market_type(
    region_id: int = Path(...),
    type_id: int = Path(...),
):
    """Get detailed market stats for a specific type in a region."""
    global all_orders

    # Validate type exists
    type_name = get_type_name(type_id)
    if type_name is None:
        return JSONResponse(
            status_code=404,
            content={"error": "UNKNOWN_ITEMS", "details": f"Unknown type ID: {type_id}"}
        )

    # Validate region exists
    region_name = get_region_name(region_id)
    if region_name is None:
        return JSONResponse(
            status_code=404,
            content={"error": "UNKNOWN_ITEMS", "details": f"Unknown region ID: {region_id}"}
        )

    # Filter orders for this region and type
    region_type_orders = []
    for order in all_orders:
        location_id = order.get("location_id")
        order_region = get_location_region(location_id)
        if order_region == region_id and order["type_id"] == type_id:
            region_type_orders.append(order)

    # Separate buy and sell orders
    buy_orders = [o for o in region_type_orders if o.get("is_buy_order", False)]
    sell_orders = [o for o in region_type_orders if not o.get("is_buy_order", False)]

    # Calculate thresholds (before filtering)
    buy_threshold = None
    sell_threshold = None

    if buy_orders:
        max_buy = max(o["price"] for o in buy_orders)
        buy_threshold = max_buy * 0.10

    if sell_orders:
        min_sell = min(o["price"] for o in sell_orders)
        sell_threshold = min_sell * 10

    # Filter outliers
    filtered_buy = []
    for o in buy_orders:
        if buy_threshold is not None and o["price"] > buy_threshold:
            filtered_buy.append(o)

    filtered_sell = []
    for o in sell_orders:
        if sell_threshold is not None and o["price"] < sell_threshold:
            filtered_sell.append(o)

    # Calculate prices
    buy_price = max((o["price"] for o in filtered_buy), default=None)
    sell_price = min((o["price"] for o in filtered_sell), default=None)

    buy_5pct = calculate_5pct_price(filtered_buy, is_buy=True)
    sell_5pct = calculate_5pct_price(filtered_sell, is_buy=False)

    # Calculate volumes
    buy_volume = sum(o["volume_remain"] for o in filtered_buy)
    sell_volume = sum(o["volume_remain"] for o in filtered_sell)

    # Calculate total value
    total_value = 0.0
    if sell_5pct is not None:
        total_value = sell_5pct * sell_volume

    response = {
        "name": type_name,
        "buy": buy_price,
        "sell": sell_price,
        "buy_5pct": buy_5pct,
        "sell_5pct": sell_5pct,
        "buy_orders": len(filtered_buy),
        "sell_orders": len(filtered_sell),
        "buy_threshold": buy_threshold,
        "sell_threshold": sell_threshold,
        "buy_volume": buy_volume,
        "sell_volume": sell_volume,
        "total_value": total_value,
    }

    return JSONResponse(status_code=200, content=response)


@app.get("/v1/hub-compare/{type_id}")
async def hub_compare(type_id: int = Path(...)):
    """Compare market stats for a type across all main hubs."""
    global market_data, all_orders

    # Validate type exists
    type_name = get_type_name(type_id)
    if type_name is None:
        return JSONResponse(
            status_code=404,
            content={"error": "UNKNOWN_ITEMS", "details": f"Unknown type ID: {type_id}"}
        )

    response = {}
    has_data = False

    for hub_name, hub_info in HUB_STATIONS.items():
        station_id = hub_info["station_id"]

        # Get orders for this type at this hub
        hub_orders = []
        for order in all_orders:
            if order["type_id"] == type_id and order["location_id"] == station_id:
                hub_orders.append(order)

        if not hub_orders:
            # No data for this hub - skip entirely
            continue

        has_data = True

        # Separate buy and sell
        buy_orders = [o for o in hub_orders if o.get("is_buy_order", False)]
        sell_orders = [o for o in hub_orders if not o.get("is_buy_order", False)]

        # Calculate thresholds
        buy_threshold = None
        sell_threshold = None

        if buy_orders:
            max_buy = max(o["price"] for o in buy_orders)
            buy_threshold = max_buy * 0.10

        if sell_orders:
            min_sell = min(o["price"] for o in sell_orders)
            sell_threshold = min_sell * 10

        # Filter outliers
        filtered_buy = [o for o in buy_orders if buy_threshold is None or o["price"] > buy_threshold]
        filtered_sell = [o for o in sell_orders if sell_threshold is None or o["price"] < sell_threshold]

        # Calculate prices
        buy_price = max((o["price"] for o in filtered_buy), default=None)
        sell_price = min((o["price"] for o in filtered_sell), default=None)

        buy_5pct = calculate_5pct_price(filtered_buy, is_buy=True)
        sell_5pct = calculate_5pct_price(filtered_sell, is_buy=False)

        # Calculate volumes
        buy_volume = sum(o["volume_remain"] for o in filtered_buy)
        sell_volume = sum(o["volume_remain"] for o in filtered_sell)

        # Calculate value (5pct sell * sell volume, in billions)
        value = None
        if sell_5pct is not None and sell_volume > 0:
            value = round((sell_5pct * sell_volume) / 1e9, 2)

        response[f"{hub_name}_sell"] = sell_price
        response[f"{hub_name}_buy"] = buy_price
        response[f"{hub_name}_sell_volume"] = sell_volume if sell_volume > 0 else None
        response[f"{hub_name}_buy_volume"] = buy_volume if buy_volume > 0 else None
        response[f"{hub_name}_value"] = value
        response[f"{hub_name}_sell_5pct"] = sell_5pct
        response[f"{hub_name}_buy_5pct"] = buy_5pct

    if not has_data:
        return JSONResponse(
            status_code=404,
            content={"error": "NO_PRICE_DATA", "details": f"No price data found for type ID {type_id} in any hub"}
        )

    return JSONResponse(status_code=200, content=response)


def main():
    parser = argparse.ArgumentParser(description="Market Tools API")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on")
    parser.add_argument("--address", type=str, default="0.0.0.0", help="Address to bind to")
    parser.add_argument("--sde", type=str, required=True, help="Path to SDE directory")

    args = parser.parse_args()

    # Load SDE data
    print(f"Loading SDE data from {args.sde}...")
    load_sde(args.sde)
    print(f"Loaded {len(sde_data['types'])} types, {len(sde_data['regions'])} regions, "
          f"{len(sde_data['systems'])} systems, {len(sde_data['stations'])} stations")

    # Start server
    import uvicorn
    uvicorn.run(app, host=args.address, port=args.port)


if __name__ == "__main__":
    main()
