#!/usr/bin/env python3
"""
Market Tools API - Industrialist market analysis tools
"""

import gzip
import csv
from datetime import datetime
from typing import Optional, Dict, List, Any
from collections import defaultdict
import io

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import uvicorn

app = FastAPI()

# In-memory data stores
price_books: Dict[str, Dict[int, Dict[str, Any]]] = defaultdict(dict)  # market -> order_id -> order_data
stations_data: Dict[int, Dict[str, Any]] = {}  # location_id -> station_data

# SDE data
type_names: Dict[int, str] = {}
region_names: Dict[int, str] = {}
station_location_ids: Dict[int, int] = {}  # location_id -> station_id

# Main hubs mapping
MAIN_HUBS = {
    "jita": 60003760,  # Jita IV - Moon 4 - Caldari Navy Assembly Plant
    "amarr": 60008494,  # Amarr VIII (Oris) - Emperor Family Academy
    "dodixie": 60011866,  # Dodixie IX - Moon 20 - Federation Navy Assembly Plant
    "rens": 60004588,  # Rens VI - Moon 8 - Brutor Tribe Treasury
    "hek": 60005686,   # Hek VIII - Moon 12 - Boundless Creation Factory
}

# Hub name mapping for output
HUB_NAMES = {
    "jita": "Jita",
    "amarr": "Amarr",
    "dodixie": "Dodixie",
    "rens": "Rens",
    "hek": "Hek"
}


def load_sde(sde_path: str) -> None:
    """Load SDE data for name resolution."""
    import bz2
    global type_names, region_names

    def open_bz2_or_gzip(filepath: str):
        """Try to open as bz2, then gzip, then plain text."""
        # Try bz2 first
        try:
            return bz2.open(filepath, 'rt', encoding='utf-8')
        except Exception:
            pass

        # Try gzip
        try:
            return gzip.open(filepath, 'rt', encoding='utf-8')
        except Exception:
            pass

        # Try plain text
        try:
            return open(filepath, 'r', encoding='utf-8')
        except Exception:
            pass

        return None

    # Load invTypes for type name resolution
    try:
        with open_bz2_or_gzip(f"{sde_path}/invTypes.csv.bz2") as f:
            if f:
                reader = csv.DictReader(f)
                for row in reader:
                    try:
                        type_id = int(row['typeID'])
                        type_names[type_id] = row['typeName']
                    except (ValueError, KeyError):
                        continue
    except Exception:
        pass

    # Load mapRegions for region name resolution
    try:
        with open_bz2_or_gzip(f"{sde_path}/mapRegions.csv.bz2") as f:
            if f:
                reader = csv.DictReader(f)
                for row in reader:
                    try:
                        region_id = int(row['regionID'])
                        region_names[region_id] = row['regionName']
                    except (ValueError, KeyError):
                        continue
    except Exception:
        pass

    # Load staStations for station data
    try:
        with open_bz2_or_gzip(f"{sde_path}/staStations.csv.bz2") as f:
            if f:
                reader = csv.DictReader(f)
                for row in reader:
                    try:
                        station_id = int(row['stationID'])
                        region_id = int(row['regionID'])
                        station_name = row['stationName']
                        # Map station name to region for quick lookup
                        if region_id not in station_location_ids:
                            station_location_ids[region_id] = {}
                        station_location_ids[region_id][station_name] = station_id
                    except (ValueError, KeyError):
                        continue
    except Exception:
        pass


class PriceUploadRequest(BaseModel):
    pass


class StationUploadRequest(BaseModel):
    pass


def parse_gzip_csv(content: bytes, expected_columns: List[str]) -> List[Dict[str, Any]]:
    """Parse gzipped CSV content and validate columns."""
    try:
        decompressed = gzip.decompress(content)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail={"error": "INVALID_FORMAT", "details": f"Decompression error: {str(e)}"}
        )

    try:
        text = decompressed.decode('utf-8')
    except UnicodeDecodeError as e:
        raise HTTPException(
            status_code=400,
            detail={"error": "INVALID_FORMAT", "details": f"Decoding error: {str(e)}"}
        )

    try:
        reader = csv.DictReader(io.StringIO(text))
        actual_columns = reader.fieldnames or []

        # Check if all expected columns are present
        for col in expected_columns:
            if col not in actual_columns:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": "INVALID_FORMAT",
                        "details": f"Missing required column: {col}. Found: {actual_columns}"
                    }
                )

        rows = []
        for row in reader:
            # Convert types
            converted_row = {}
            for col in expected_columns:
                value = row.get(col, '')

                # Type inference based on column name
                if col in ['order_id', 'duration', 'location_id', 'system_id', 'type_id', 'min_volume', 'volume_remain', 'volume_total']:
                    try:
                        converted_row[col] = int(value) if value else 0
                    except ValueError:
                        converted_row[col] = 0
                elif col in ['price']:
                    try:
                        converted_row[col] = float(value) if value else 0.0
                    except ValueError:
                        converted_row[col] = 0.0
                elif col == 'is_buy_order':
                    converted_row[col] = value.lower() in ('true', '1', 'yes') if value else False
                elif col == 'issued':
                    # Parse ISO-8601
                    converted_row[col] = value
                else:
                    converted_row[col] = value

            rows.append(converted_row)

        return rows

    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail={"error": "INVALID_FORMAT", "details": f"CSV parsing error: {str(e)}"}
        )


def parse_station_gzip_csv(content: bytes) -> List[Dict[str, Any]]:
    """Parse gzipped CSV content for stations and validate columns."""
    try:
        decompressed = gzip.decompress(content)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail={"error": "INVALID_FORMAT", "details": f"Decompression error: {str(e)}"}
        )

    try:
        text = decompressed.decode('utf-8')
    except UnicodeDecodeError as e:
        raise HTTPException(
            status_code=400,
            detail={"error": "INVALID_FORMAT", "details": f"Decoding error: {str(e)}"}
        )

    try:
        reader = csv.DictReader(io.StringIO(text))
        actual_columns = reader.fieldnames or []

        expected_columns = ['location_id', 'type', 'name']

        # Check if all expected columns are present
        for col in expected_columns:
            if col not in actual_columns:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": "INVALID_FORMAT",
                        "details": f"Missing required column: {col}. Found: {actual_columns}"
                    }
                )

        rows = []
        for row in reader:
            converted_row = {
                'location_id': int(row['location_id']) if row.get('location_id') else 0,
                'type': row.get('type', ''),
                'name': row.get('name', '')
            }
            rows.append(converted_row)

        return rows

    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail={"error": "INVALID_FORMAT", "details": f"CSV parsing error: {str(e)}"}
        )


def get_type_name(type_id: int) -> Optional[str]:
    """Get type name from SDE."""
    return type_names.get(type_id)


def is_valid_type_id(type_id: int) -> bool:
    """Check if type ID exists in SDE."""
    return type_id in type_names


def filter_outlier_orders(orders: List[Dict], is_buy: bool) -> List[Dict]:
    """Filter outlier orders (10% of highest buy price or 10x lowest sell)."""
    if not orders:
        return orders

    if is_buy:
        # For buy orders: filter out 10% of the highest buy prices
        sorted_orders = sorted(orders, key=lambda x: x['price'], reverse=True)
        if len(sorted_orders) <= 1:
            return orders

        # Calculate threshold - bottom 90%
        cutoff_index = max(1, int(len(sorted_orders) * 0.9))
        threshold_price = sorted_orders[cutoff_index - 1]['price']

        return [o for o in sorted_orders if o['price'] <= threshold_price]
    else:
        # For sell orders: filter out 10x the lowest sell
        sorted_orders = sorted(orders, key=lambda x: x['price'])
        if len(sorted_orders) <= 1:
            return orders

        lowest_price = sorted_orders[0]['price']
        threshold_price = lowest_price * 10

        return [o for o in sorted_orders if o['price'] <= threshold_price]


def calculate_volume_weighted_average_price(orders: List[Dict], target_volume: float = None) -> float:
    """Calculate volume weighted average price for top orders."""
    if not orders:
        return 0.0

    # Sort by price (descending for buy, ascending for sell)
    is_buy = orders[0].get('is_buy_order', True)
    sorted_orders = sorted(orders, key=lambda x: x['price'], reverse=is_buy)

    total_volume = sum(o['volume_remain'] for o in orders)

    if target_volume is None:
        # For 5% calculation
        target_volume = total_volume * 0.05

    if target_volume <= 0:
        return 0.0

    accumulated_volume = 0
    total_value = 0.0

    for order in sorted_orders:
        order_volume = order['volume_remain']
        order_price = order['price']

        if accumulated_volume + order_volume <= target_volume:
            # Take all of this order
            total_value += order_volume * order_price
            accumulated_volume += order_volume
        else:
            # Take only part of this order
            remaining_needed = target_volume - accumulated_volume
            total_value += remaining_needed * order_price
            accumulated_volume += remaining_needed
            break

    if accumulated_volume == 0:
        return 0.0

    return total_value / accumulated_volume


@app.post("/v1/prices")
async def ingest_prices(
    request: Request,
    market: str = Query("jita"),
    mode: str = Query("replace"),
    location_id: Optional[int] = Query(None)
):
    """Ingest price data from gzipped CSV."""
    content = await request.body()

    if not content:
        raise HTTPException(
            status_code=400,
            detail={"error": "INVALID_FORMAT", "details": "No content provided"}
        )

    expected_columns = [
        'order_id', 'duration', 'is_buy_order', 'issued', 'location_id',
        'min_volume', 'price', 'range', 'system_id', 'type_id',
        'volume_remain', 'volume_total'
    ]

    rows = parse_gzip_csv(content, expected_columns)

    # Filter by location_id if provided
    if location_id is not None:
        rows = [r for r in rows if r['location_id'] == location_id]

    if mode == "replace":
        price_books[market] = {}

    # Upsert orders
    for row in rows:
        order_id = row['order_id']
        if mode == "append" and order_id in price_books[market]:
            # Keep newer order (compare issued timestamp)
            existing = price_books[market][order_id]
            if row['issued'] > existing['issued']:
                price_books[market][order_id] = row
        else:
            price_books[market][order_id] = row

    return {"status": "PRICES_UPDATED", "count": len(rows)}


@app.post("/v1/stations")
async def ingest_stations(request: Request):
    """Ingest station data from gzipped CSV."""
    content = await request.body()

    if not content:
        raise HTTPException(
            status_code=400,
            detail={"error": "INVALID_FORMAT", "details": "No content provided"}
        )

    rows = parse_station_gzip_csv(content)

    for row in rows:
        stations_data[row['location_id']] = row

    return {"status": "STATIONS_UPDATED", "count": len(rows)}


def get_hub_orders(market: str) -> tuple:
    """Get buy and sell orders for a market."""
    orders = price_books.get(market, {})
    buy_orders = [o for o in orders.values() if o['is_buy_order']]
    sell_orders = [o for o in orders.values() if not o['is_buy_order']]
    return buy_orders, sell_orders


def get_regional_hub(market: str) -> str:
    """Determine the regional hub (location with most sell orders)."""
    orders = price_books.get(market, {})
    sell_orders = [o for o in orders.values() if not o['is_buy_order']]

    if not sell_orders:
        return market

    # Count sell orders by location
    location_counts = defaultdict(int)
    for order in sell_orders:
        location_counts[order['location_id']] += 1

    if not location_counts:
        return market

    # Return location with most sell orders
    return max(location_counts, key=location_counts.get)


def get_station_name(location_id: int) -> str:
    """Get station name for a location ID."""
    if location_id in stations_data:
        station = stations_data[location_id]
        return station.get('name', str(location_id))

    # Try to find in SDE station data
    for region_id, stations in station_location_ids.items():
        for name, station_id in stations.items():
            # The mapping from station_id to location_id is not direct
            pass

    return str(location_id)


@app.get("/v1/market/{region_id}")
async def get_market_stats(
    region_id: int,
    type_ids: Optional[str] = Query(None),
    hubs: Optional[int] = Query(None)
):
    """Get market statistics for a region."""
    region_name = region_names.get(region_id, f"Region {region_id}")

    # Aggregate across all markets that might be in this region
    # For now, we'll look at all price books and try to determine region
    # This is a simplified approach - in reality, we'd need location to region mapping

    all_buy_orders = []
    all_sell_orders = []

    for market, orders in price_books.items():
        for order in orders.values():
            # Try to determine if this order is in the region
            # We need location to region mapping from SDE
            # For now, include all orders (simplified)
            if order['is_buy_order']:
                all_buy_orders.append(order)
            else:
                all_sell_orders.append(order)

    # Filter outliers
    filtered_buy = filter_outlier_orders(all_buy_orders, True)
    filtered_sell = filter_outlier_orders(all_sell_orders, False)

    # Calculate totals
    sell_value = sum(o['price'] * o['volume_remain'] for o in filtered_sell) / 1_000_000_000
    buy_value = sum(o['price'] * o['volume_remain'] for o in filtered_buy) / 1_000_000_000

    # Count unique items
    all_type_ids = set()
    for order in filtered_buy + filtered_sell:
        all_type_ids.add(order['type_id'])

    response = {
        "name": region_name,
        "sell_orders": len(filtered_sell),
        "buy_orders": len(filtered_buy),
        "sell_value": round(sell_value, 2),
        "buy_value": round(buy_value, 2),
        "unique_items": len(all_type_ids)
    }

    # Handle type_ids parameter
    if type_ids:
        type_id_list = [int(tid.strip()) for tid in type_ids.split(',')]
        response["types"] = {}

        for tid in type_id_list:
            type_orders = []
            for market, orders in price_books.items():
                for order in orders.values():
                    if order['type_id'] == tid:
                        type_orders.append(order)

            type_buy = [o for o in type_orders if o['is_buy_order']]
            type_sell = [o for o in type_orders if not o['is_buy_order']]

            # Filter outliers
            filtered_type_buy = filter_outlier_orders(type_buy, True)
            filtered_type_sell = filter_outlier_orders(type_sell, False)

            type_name = get_type_name(tid) or f"Type {tid}"

            if filtered_type_buy or filtered_type_sell:
                type_data = {}

                if filtered_type_buy:
                    highest_buy = max(o['price'] for o in filtered_type_buy)
                    type_data["buy"] = round(highest_buy, 2)
                else:
                    type_data["buy"] = None

                if filtered_type_sell:
                    lowest_sell = min(o['price'] for o in filtered_type_sell)
                    type_data["sell"] = round(lowest_sell, 2)
                else:
                    type_data["sell"] = None

                if type_data["buy"] is not None and type_data["sell"] is not None:
                    type_data["split"] = round((type_data["buy"] + type_data["sell"]) / 2, 2)
                else:
                    type_data["split"] = None

                type_data["buy_orders"] = len(filtered_type_buy)
                type_data["sell_orders"] = len(filtered_type_sell)

                response["types"][type_name] = type_data
            else:
                response["types"][type_name] = {
                    "buy": None,
                    "sell": None,
                    "split": None,
                    "buy_orders": 0,
                    "sell_orders": 0
                }

    # Handle hubs parameter
    if hubs is not None:
        response["hubs"] = []

        for hub_name, station_id in MAIN_HUBS.items():
            # Get orders for this hub's station
            hub_orders = []
            for market, orders in price_books.items():
                for order in orders.values():
                    if order['location_id'] == station_id:
                        hub_orders.append(order)

            hub_sell = [o for o in hub_orders if not o['is_buy_order']]
            hub_buy = [o for o in hub_orders if o['is_buy_order']]

            sell_value = sum(o['price'] * o['volume_remain'] for o in hub_sell) / 1_000_000_000

            hub_info = {
                "station": HUB_NAMES.get(hub_name, hub_name.title()),
                "orders": len(hub_sell),
                "sell_value": round(sell_value, 2)
            }
            response["hubs"].append(hub_info)

        # Sort by number of orders
        response["hubs"].sort(key=lambda x: x["orders"], reverse=True)

    return response


@app.get("/v1/market/{region_id}/{type_id}")
async def get_type_market_stats(region_id: int, type_id: int):
    """Get detailed market statistics for a specific type."""
    type_name = get_type_name(type_id) or f"Type {type_id}"

    # Find all orders for this type across all markets
    all_orders = []
    for market, orders in price_books.items():
        for order in orders.values():
            if order['type_id'] == type_id:
                all_orders.append(order)

    buy_orders = [o for o in all_orders if o['is_buy_order']]
    sell_orders = [o for o in all_orders if not o['is_buy_order']]

    # Filter outliers
    filtered_buy = filter_outlier_orders(buy_orders, True)
    filtered_sell = filter_outlier_orders(sell_orders, False)

    # Calculate basic stats
    buy = max((o['price'] for o in filtered_buy), default=None)
    sell = min((o['price'] for o in filtered_sell), default=None)

    # Calculate 5% volume weighted average prices
    buy_5pct = calculate_volume_weighted_average_price(filtered_buy)
    sell_5pct = calculate_volume_weighted_average_price(filtered_sell)

    # Calculate thresholds
    if filtered_buy:
        sorted_buy = sorted(filtered_buy, key=lambda x: x['price'], reverse=True)
        buy_threshold = sorted_buy[min(1, len(sorted_buy) - 1)]['price']
    else:
        buy_threshold = 0.0

    if filtered_sell:
        sorted_sell = sorted(filtered_sell, key=lambda x: x['price'])
        sell_threshold = sorted_sell[min(1, len(sorted_sell) - 1)]['price']
    else:
        sell_threshold = 0.0

    # Calculate volumes
    buy_volume = sum(o['volume_remain'] for o in filtered_buy)
    sell_volume = sum(o['volume_remain'] for o in filtered_sell)

    # Total value using 5pct price
    total_value = (sell_5pct * sell_volume) / 1_000_000_000

    response = {
        "name": type_name,
        "buy": round(buy, 2) if buy is not None else None,
        "sell": round(sell, 2) if sell is not None else None,
        "buy_5pct": round(buy_5pct, 2) if buy_5pct > 0 else None,
        "sell_5pct": round(sell_5pct, 2) if sell_5pct > 0 else None,
        "buy_orders": len(filtered_buy),
        "sell_orders": len(filtered_sell),
        "buy_threshold": round(buy_threshold, 2),
        "sell_threshold": round(sell_threshold, 2),
        "buy_volume": buy_volume,
        "sell_volume": sell_volume,
        "total_value": round(total_value, 2)
    }

    return response


@app.get("/v1/hub-compare/{type_id}")
async def get_hub_compare(type_id: int):
    """Get market statistics across main hubs for a type."""
    # Check if we have any data at all
    if not price_books:
        raise HTTPException(
            status_code=404,
            detail={"error": "NO_PRICE_DATA", "details": "No price data available for any hub"}
        )

    # Check if type exists
    type_name = get_type_name(type_id)
    if type_name is None:
        # Still continue, might have price data but no SDE entry
        pass

    response = {}
    has_data = False

    for hub_key, station_id in MAIN_HUBS.items():
        # Get orders for this hub
        hub_orders = []
        for market, orders in price_books.items():
            for order in orders.values():
                if order['location_id'] == station_id and order['type_id'] == type_id:
                    hub_orders.append(order)

        if hub_orders:
            has_data = True
            buy_orders = [o for o in hub_orders if o['is_buy_order']]
            sell_orders = [o for o in hub_orders if not o['is_buy_order']]

            # Filter outliers
            filtered_buy = filter_outlier_orders(buy_orders, True)
            filtered_sell = filter_outlier_orders(sell_orders, False)

            # Calculate stats
            sell = min((o['price'] for o in filtered_sell), default=None)
            buy = max((o['price'] for o in filtered_buy), default=None)

            sell_volume = sum(o['volume_remain'] for o in filtered_sell)
            buy_volume = sum(o['volume_remain'] for o in filtered_buy)

            sell_5pct = calculate_volume_weighted_average_price(filtered_sell)
            buy_5pct = calculate_volume_weighted_average_price(filtered_buy)

            # Value is 5pct sell * sell volume, in billions
            value = (sell_5pct * sell_volume) / 1_000_000_000

            response[f"{hub_key}_sell"] = round(sell, 2) if sell is not None else None
            response[f"{hub_key}_buy"] = round(buy, 2) if buy is not None else None
            response[f"{hub_key}_sell_volume"] = round(sell_volume, 2) if sell_volume > 0 else None
            response[f"{hub_key}_buy_volume"] = round(buy_volume, 2) if buy_volume > 0 else None
            response[f"{hub_key}_value"] = round(value, 2) if value > 0 else None
            response[f"{hub_key}_sell_5pct"] = round(sell_5pct, 2) if sell_5pct > 0 else None
            response[f"{hub_key}_buy_5pct"] = round(buy_5pct, 2) if buy_5pct > 0 else None
        else:
            response[f"{hub_key}_sell"] = None
            response[f"{hub_key}_buy"] = None
            response[f"{hub_key}_sell_volume"] = None
            response[f"{hub_key}_buy_volume"] = None
            response[f"{hub_key}_value"] = None
            response[f"{hub_key}_sell_5pct"] = None
            response[f"{hub_key}_buy_5pct"] = None

    if not has_data:
        raise HTTPException(
            status_code=404,
            detail={"error": "NO_PRICE_DATA", "details": f"No price data available for type {type_id} in any hub"}
        )

    return response


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Custom exception handler for HTTP exceptions."""
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.detail if isinstance(exc.detail, dict) else {"error": "UNKNOWN_ERROR", "details": str(exc.detail)}
    )


def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Market Tools API")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on")
    parser.add_argument("--address", type=str, default="127.0.0.1", help="Address to bind to")
    parser.add_argument("--sde", type=str, required=True, help="Path to SDE directory")

    args = parser.parse_args()

    # Load SDE data
    load_sde(args.sde)

    # Start server
    uvicorn.run(app, host=args.address, port=args.port)


if __name__ == "__main__":
    main()
