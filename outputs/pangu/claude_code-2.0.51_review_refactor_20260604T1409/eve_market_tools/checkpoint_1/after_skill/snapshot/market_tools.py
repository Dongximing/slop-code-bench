#!/usr/bin/env python3
"""
Market Tools API for industrialists.
Ingest raw orders, build price books, query quotes, value inventories,
and reason about depth and spreads across markets.
"""

import argparse
import csv
import gzip
import io
from datetime import datetime
from collections import defaultdict
from typing import Optional, Dict, List, Any

from dateutil.parser import isoparse
from flask import Flask, request, jsonify

from sde_loader import SDELoader

app = Flask(__name__)

# Global storage
price_books: Dict[str, Dict[int, Dict[int, List[Dict]]]] = defaultdict(
    lambda: defaultdict(dict)
)
stations: Dict[int, Dict[str, Any]] = {}

# Market name to location ID mapping for main hubs
MARKET_HUB_NAMES = {
    "jita": "Jita IV - Moon 4 - Caldari Navy Assembly Plant",
    "amarr": "Amarr VIII (Oris) - Emperor Family Academy",
    "dodixie": "Dodixie IX - Moon 20 - Federation Navy Assembly Plant",
    "rens": "Rens VI - Moon 8 - Brutor Tribe Treasury",
    "hek": "Hek VIII - Moon 12 - Boundless Creation Factory",
}

# Initialize SDE loader (will be set at runtime)
sde_loader: Optional[SDELoader] = None


def get_market_hubs() -> Dict[str, str]:
    """Get mapping of hub keys to their full station names."""
    return MARKET_HUB_NAMES


def calc_5pct_price(orders: List[Dict], is_buy: bool) -> Optional[float]:
    """Calculate 5% volume-weighted average price for filtered orders."""
    if not orders:
        return None

    # Sort by price (descending for buy, ascending for sell)
    if is_buy:
        sorted_orders = sorted(orders, key=lambda x: -x['price'])
    else:
        sorted_orders = sorted(orders, key=lambda x: x['price'])

    total_volume = sum(o['volume_remain'] for o in sorted_orders)
    target_volume = total_volume * 0.05

    accumulated = 0
    weighted_sum = 0.0

    for order in sorted_orders:
        vol = order['volume_remain']
        price = order['price']

        if accumulated + vol <= target_volume:
            weighted_sum += vol * price
            accumulated += vol
        else:
            remaining = target_volume - accumulated
            weighted_sum += remaining * price
            accumulated += remaining
            break

    if accumulated == 0:
        return None

    return weighted_sum / accumulated


# ============================================================================
# Price Ingestion Endpoint
# ============================================================================

@app.route('/v1/prices', methods=['POST'])
def ingest_prices():
    """
    Ingest gzipped CSV price data and build/update price books.
    Query options:
      - market: logical name for price book (default: "jita")
      - mode: "replace" or "append" (default: "replace")
      - location_id: optional override to filter by specific location
    """
    # Get query parameters
    market_name = request.args.get('market', 'jita')
    mode = request.args.get('mode', 'replace')
    location_id_str = request.args.get('location_id')

    # Validate mode
    if mode not in ('replace', 'append'):
        return jsonify({
            "error": "INVALID_FORMAT",
            "details": f"Invalid mode '{mode}'. Must be 'replace' or 'append'."
        }), 400

    # Validate content type
    if not request.data:
        return jsonify({
            "error": "INVALID_FORMAT",
            "details": "No data provided. Expected gzipped CSV bytes."
        }), 400

    # Decompress gzip data
    try:
        decompressed = gzip.decompress(request.data)
        csv_bytes = io.BytesIO(decompressed)
        csv_text = csv_bytes.getvalue().decode('utf-8')
        csv_file = io.StringIO(csv_text)
    except gzip.BadGzipFile:
        return jsonify({
            "error": "INVALID_FORMAT",
            "details": "Invalid gzip data. Expected gzipped CSV bytes."
        }), 400
    except UnicodeDecodeError:
        return jsonify({
            "error": "INVALID_FORMAT",
            "details": "Unable to decode CSV as UTF-8."
        }), 400
    except Exception as e:
        return jsonify({
            "error": "INVALID_FORMAT",
            "details": f"Error processing compressed data: {str(e)}"
        }), 400

    # Parse CSV and validate schema
    try:
        reader = csv.DictReader(csv_file)
        required_columns = {
            'order_id', 'duration', 'is_buy_order', 'issued', 'location_id',
            'min_volume', 'price', 'range', 'system_id', 'type_id',
            'volume_remain', 'volume_total'
        }

        if not reader.fieldnames:
            return jsonify({
                "error": "INVALID_FORMAT",
                "details": "CSV file is empty or has no headers."
            }), 400

        actual_columns = set(reader.fieldnames)
        if not required_columns.issubset(actual_columns):
            missing = required_columns - actual_columns
            return jsonify({
                "error": "INVALID_FORMAT",
                "details": f"Missing required columns: {', '.join(sorted(missing))}"
            }), 400

        rows = list(reader)
    except csv.Error as e:
        return jsonify({
            "error": "INVALID_FORMAT",
            "details": f"CSV parsing error: {str(e)}"
        }), 400
    except Exception as e:
        return jsonify({
            "error": "INVALID_FORMAT",
            "details": f"Error reading CSV: {str(e)}"
        }), 400

    # Parse and process rows
    order_list = []
    errors = []

    for i, row in enumerate(rows):
        try:
            order = {
                'order_id': int(row['order_id']),
                'duration': int(row['duration']),
                'is_buy_order': row['is_buy_order'].lower() in ('true', '1', 'yes'),
                'issued': isoparse(row['issued']),
                'location_id': int(row['location_id']),
                'min_volume': int(row['min_volume']),
                'price': float(row['price']),
                'range': row['range'],
                'system_id': int(row['system_id']),
                'type_id': int(row['type_id']),
                'volume_remain': int(row['volume_remain']),
                'volume_total': int(row['volume_total']),
            }

            # Check location filter if provided
            if location_id_str is not None:
                try:
                    location_filter = int(location_id_str)
                    if order['location_id'] != location_filter:
                        continue
                except ValueError:
                    errors.append(f"Row {i+1}: Invalid location_id format")
                    continue

            order_list.append(order)
        except (ValueError, TypeError) as e:
            errors.append(f"Row {i+1}: {str(e)}")
            continue

    if errors and not order_list:
        return jsonify({
            "error": "INVALID_FORMAT",
            "details": "; ".join(errors)
        }), 400
    elif errors:
        # Log but continue - we have some valid orders
        pass

    # Get or create market price book
    if mode == 'replace' or market_name not in price_books:
        price_books[market_name] = defaultdict(dict)

    # Process orders by type_id and location_id
    for order in order_list:
        type_id = order['type_id']
        location_id = order['location_id']

        # Append or replace based on mode
        if mode == 'append':
            # Check if order_id exists, if newer issued wins
            existing = price_books[market_name][type_id].get(location_id, [])
            order_id_exists = False

            for j, existing_order in enumerate(existing):
                if existing_order['order_id'] == order['order_id']:
                    if order['issued'] > existing_order['issued']:
                        existing[j] = order
                    order_id_exists = True
                    break

            if not order_id_exists:
                existing.append(order)

            price_books[market_name][type_id][location_id] = existing
        else:
            # Replace mode - just overwrite
            if location_id not in price_books[market_name][type_id]:
                price_books[market_name][type_id][location_id] = []
            price_books[market_name][type_id][location_id].append(order)

    return jsonify({
        "status": "PRICES_UPDATED",
        "count": len(order_list)
    }), 200


# ============================================================================
# Station Ingestion Endpoint
# ============================================================================

@app.route('/v1/stations', methods=['POST'])
def ingest_stations():
    """
    Ingest gzipped CSV station data.
    Required columns: location_id, type, name
    """
    # Validate content type
    if not request.data:
        return jsonify({
            "error": "INVALID_FORMAT",
            "details": "No data provided. Expected gzipped CSV bytes."
        }), 400

    # Decompress gzip data
    try:
        decompressed = gzip.decompress(request.data)
        csv_bytes = io.BytesIO(decompressed)
        csv_text = csv_bytes.getvalue().decode('utf-8')
        csv_file = io.StringIO(csv_text)
    except gzip.BadGzipFile:
        return jsonify({
            "error": "INVALID_FORMAT",
            "details": "Invalid gzip data. Expected gzipped CSV bytes."
        }), 400
    except UnicodeDecodeError:
        return jsonify({
            "error": "INVALID_FORMAT",
            "details": "Unable to decode CSV as UTF-8."
        }), 400
    except Exception as e:
        return jsonify({
            "error": "INVALID_FORMAT",
            "details": f"Error processing compressed data: {str(e)}"
        }), 400

    # Parse CSV and validate schema
    try:
        reader = csv.DictReader(csv_file)
        required_columns = {'location_id', 'type', 'name'}

        if not reader.fieldnames:
            return jsonify({
                "error": "INVALID_FORMAT",
                "details": "CSV file is empty or has no headers."
            }), 400

        actual_columns = set(reader.fieldnames)
        if not required_columns.issubset(actual_columns):
            missing = required_columns - actual_columns
            return jsonify({
                "error": "INVALID_FORMAT",
                "details": f"Missing required columns: {', '.join(sorted(missing))}"
            }), 400

        rows = list(reader)
    except csv.Error as e:
        return jsonify({
            "error": "INVALID_FORMAT",
            "details": f"CSV parsing error: {str(e)}"
        }), 400
    except Exception as e:
        return jsonify({
            "error": "INVALID_FORMAT",
            "details": f"Error reading CSV: {str(e)}"
        }), 400

    # Process rows
    count = 0
    errors = []

    for i, row in enumerate(rows):
        try:
            location_id = int(row['location_id'])
            location_type = row['type']
            name = row['name']

            # Validate type
            if location_type not in ('Station', 'Structure'):
                errors.append(f"Row {i+1}: Invalid type '{location_type}'. Must be 'Station' or 'Structure'.")
                continue

            stations[location_id] = {
                'location_id': location_id,
                'type': location_type,
                'name': name,
                'region_id': None
            }
            count += 1
        except (ValueError, TypeError) as e:
            errors.append(f"Row {i+1}: {str(e)}")
            continue

    if errors and not count:
        return jsonify({
            "error": "INVALID_FORMAT",
            "details": "; ".join(errors)
        }), 400

    return jsonify({
        "status": "STATIONS_UPDATED",
        "count": count
    }), 200


# ============================================================================
# Market Statistics Functions
# ============================================================================

def get_orders_by_type_and_location(market_name: str, type_id: int, location_id: int) -> List[Dict]:
    """Get all orders for a type at a specific location."""
    return price_books.get(market_name, {}).get(type_id, {}).get(location_id, [])


def get_all_orders_by_type(market_name: str, type_id: int) -> List[Dict]:
    """Get all orders for a type across all locations."""
    all_orders = []
    for location_orders in price_books.get(market_name, {}).get(type_id, {}).values():
        all_orders.extend(location_orders)
    return all_orders


def calculate_filtered_buy_sell(orders: List[Dict]) -> tuple[List[Dict], List[Dict]]:
    """
    Filter orders to remove outliers:
    - 10% of highest buy price removed
    - 10x of lowest sell price removed
    Returns filtered buy and sell orders.
    """
    buy_orders = [o for o in orders if o['is_buy_order']]
    sell_orders = [o for o in orders if not o['is_buy_order']]

    # Filter buy orders - remove top 10% by price (highest first)
    if buy_orders:
        sorted_buys = sorted(buy_orders, key=lambda x: x['price'], reverse=True)
        threshold_idx = max(1, int(len(sorted_buys) * 0.1))
        filtered_buys = sorted_buys[threshold_idx:]
    else:
        filtered_buys = []

    # Filter sell orders - remove bottom 10x by price (lowest first)
    if sell_orders:
        sorted_sells = sorted(sell_orders, key=lambda x: x['price'])
        threshold_idx = max(1, int(len(sorted_sells) * 0.1))
        filtered_sells = sorted_sells[threshold_idx:]
    else:
        filtered_sells = []

    return filtered_buys, filtered_sells


def get_hub_orders_for_market(market_name: str) -> List[int]:
    """Get location IDs for the main hub locations based on name."""
    if sde_loader is None:
        return []

    return list(sde_loader.get_hub_location_ids(market_name))


def get_regional_hub(market_name: str, type_id: int) -> Optional[int]:
    """
    Determine the regional hub based on most sell orders.
    Returns location_id of the hub.
    """
    hub_location_ids = get_hub_orders_for_market(market_name)
    if not hub_location_ids:
        return None

    max_orders = 0
    regional_hub = None

    for location_id in hub_location_ids:
        orders = get_orders_by_type_and_location(market_name, type_id, location_id)
        sell_count = len([o for o in orders if not o['is_buy_order']])
        if sell_count > max_orders:
            max_orders = sell_count
            regional_hub = location_id

    return regional_hub


def get_top_station_by_sell_orders(market_name: str) -> tuple[Optional[str], int]:
    """Get the station with the most sell orders across all types."""
    hub_location_ids = get_hub_orders_for_market(market_name)
    if not hub_location_ids:
        return None, 0

    max_orders = 0
    best_station = None

    for location_id in hub_location_ids:
        all_orders = []
        for type_id in price_books.get(market_name, {}):
            all_orders.extend(get_orders_by_type_and_location(market_name, type_id, location_id))

        sell_count = len([o for o in all_orders if not o['is_buy_order']])
        if sell_count > max_orders:
            max_orders = sell_count
            best_station = location_id

    if best_station is not None:
        station_name = stations.get(best_station, {}).get('name')
        if not station_name and sde_loader:
            station_name = sde_loader.get_station_name(best_station)
        return station_name or str(best_station), max_orders

    return None, 0


# ============================================================================
# Regional Market Endpoint
# ============================================================================

@app.route('/v1/market/<region_id>', methods=['GET'])
def get_regional_stats(region_id: str):
    """
    Get regional market statistics.
    Query options:
      - type_ids: comma-separated list of type IDs for detailed type stats
      - hubs: include hub statistics if set to any value
    """
    # Determine market name based on region
    market_name = None
    region_id_int = None

    # Try to parse as integer
    try:
        region_id_int = int(region_id)
        market_name = None
        if sde_loader:
            region_name = sde_loader.get_region_name(region_id_int)
            if region_name:
                # Find matching market name
                for key, full_name in get_market_hubs().items():
                    if region_name.lower() in full_name.lower() or full_name.lower() in region_name.lower():
                        market_name = key
                        break
    except ValueError:
        # Treat as market hub name
        market_name = region_id.lower()
        if market_name in get_market_hubs():
            if sde_loader:
                region_id_int = list(sde_loader.region_name_to_id.values())[0]  # fallback

    # Validate market
    if market_name not in price_books and market_name not in get_market_hubs():
        return jsonify({
            "error": "UNKNOWN_ITEMS",
            "details": f"Unknown region or market hub: {region_id}"
        }), 404

    # Use market name as book key
    book_key = market_name

    # Check if we have data
    if book_key not in price_books:
        # Return empty stats if region not in price books
        region_name = "Unknown"
        if sde_loader and region_id_int:
            region_name = sde_loader.get_region_name(region_id_int) or "Unknown"
        return jsonify({
            "name": region_name,
            "sell_orders": 0,
            "buy_orders": 0,
            "sell_value": 0.0,
            "buy_value": 0.0,
            "unique_items": 0
        }), 200

    # Get all orders in this price book
    all_orders = []
    for type_id in price_books[book_key]:
        for location_id in price_books[book_key][type_id]:
            all_orders.extend(price_books[book_key][type_id][location_id])

    # Filter orders
    filtered_buys, filtered_sells = calculate_filtered_buy_sell(all_orders)

    # Calculate stats
    sell_value = sum(o['price'] * o['volume_remain'] for o in filtered_sells) / 1_000_000_000
    buy_value = sum(o['price'] * o['volume_remain'] for o in filtered_buys) / 1_000_000_000

    # Get region name
    region_name = "Unknown"
    if sde_loader and region_id_int:
        region_name = sde_loader.get_region_name(region_id_int) or "Unknown"
    elif market_name in get_market_hubs():
        region_name = market_name

    result = {
        "name": region_name,
        "sell_orders": len(filtered_sells),
        "buy_orders": len(filtered_buys),
        "sell_value": round(sell_value, 2),
        "buy_value": round(buy_value, 2),
        "unique_items": len(price_books[book_key])
    }

    # Handle type_ids parameter
    type_ids_str = request.args.get('type_ids')
    if type_ids_str:
        type_ids = [int(tid.strip()) for tid in type_ids_str.split(',') if tid.strip()]
        result['types'] = {}

        for type_id in type_ids:
            type_orders = get_all_orders_by_type(book_key, type_id)
            filtered_buys_type, filtered_sells_type = calculate_filtered_buy_sell(type_orders)

            # Determine regional hub for this type
            regional_hub = get_regional_hub(book_key, type_id)

            if regional_hub:
                hub_orders = get_orders_by_type_and_location(book_key, type_id, regional_hub)
                buys = [o for o in hub_orders if o['is_buy_order']]
                sells = [o for o in hub_orders if not o['is_buy_order']]
            else:
                buys, sells = filtered_buys_type, filtered_sells_type

            # Calculate prices
            highest_buy = max((o['price'] for o in buys), default=None)
            lowest_sell = min((o['price'] for o in sells), default=None)

            # Calculate mid point (split)
            split = None
            if highest_buy is not None and lowest_sell is not None:
                split = (highest_buy + lowest_sell) / 2

            type_name = sde_loader.get_type_name(type_id) if sde_loader else str(type_id)

            result['types'][type_name] = {
                "buy": round(highest_buy, 2) if highest_buy is not None else None,
                "sell": round(lowest_sell, 2) if lowest_sell is not None else None,
                "split": round(split, 2) if split is not None else None,
                "buy_orders": len(buys),
                "sell_orders": len(sells)
            }

    # Handle hubs parameter
    if request.args.get('hubs'):
        result['hubs'] = []
        hub_location_ids = get_hub_orders_for_market(book_key)

        hub_stats = []
        for location_id in hub_location_ids:
            all_loc_orders = []
            for type_id in price_books[book_key]:
                all_loc_orders.extend(get_orders_by_type_and_location(book_key, type_id, location_id))

            loc_buys, loc_sells = calculate_filtered_buy_sell(all_loc_orders)

            # Get station name
            station_name = stations.get(location_id, {}).get('name')
            if not station_name and sde_loader:
                station_name = sde_loader.get_station_name(location_id)
            if not station_name:
                station_name = str(location_id)

            sell_value_loc = sum(o['price'] * o['volume_remain'] for o in loc_sells) / 1_000_000_000

            hub_stats.append({
                "station": station_name,
                "orders": len(loc_sells),
                "sell_value": round(sell_value_loc, 2)
            })

        # Sort by orders descending
        hub_stats.sort(key=lambda x: x['orders'], reverse=True)
        result['hubs'] = hub_stats

    return jsonify(result), 200


# ============================================================================
# Type-Specific Market Endpoint
# ============================================================================

@app.route('/v1/market/<region_id>/<int:type_id>', methods=['GET'])
def get_type_details(region_id: str, type_id: int):
    """
    Get detailed market stats for a specific type in a region.
    """
    # Determine market name based on region
    market_name = None

    try:
        region_id_int = int(region_id)
        market_name = None
        if sde_loader:
            region_name = sde_loader.get_region_name(region_id_int)
            if region_name:
                for key, full_name in get_market_hubs().items():
                    if region_name.lower() in full_name.lower() or full_name.lower() in region_name.lower():
                        market_name = key
                        break
    except ValueError:
        market_name = region_id.lower()

    # Validate market
    if market_name not in price_books:
        return jsonify({
            "error": "UNKNOWN_ITEMS",
            "details": f"No price data for region: {region_id}"
        }), 404

    # Get all orders for this type
    all_orders = get_all_orders_by_type(market_name, type_id)

    if not all_orders:
        type_name = sde_loader.get_type_name(type_id) if sde_loader else str(type_id)
        return jsonify({
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
            "total_value": 0.0
        }), 200

    # Filter orders
    filtered_buys, filtered_sells = calculate_filtered_buy_sell(all_orders)

    # Calculate threshold prices (first price after removing outliers)
    buy_threshold = None
    sell_threshold = None

    if all_orders:
        sorted_all_buys = sorted([o for o in all_orders if o['is_buy_order']], key=lambda x: -x['price'])
        threshold_idx = max(1, int(len(sorted_all_buys) * 0.1))
        if threshold_idx < len(sorted_all_buys):
            buy_threshold = sorted_all_buys[threshold_idx]['price']

        sorted_all_sells = sorted([o for o in all_orders if not o['is_buy_order']], key=lambda x: x['price'])
        threshold_idx = max(1, int(len(sorted_all_sells) * 0.1))
        if threshold_idx < len(sorted_all_sells):
            sell_threshold = sorted_all_sells[threshold_idx]['price']

    # Calculate 5% volume-weighted average price
    buy_5pct = calc_5pct_price(filtered_buys, True)
    sell_5pct = calc_5pct_price(filtered_sells, False)

    # Get highest and lowest prices from regional hub
    regional_hub = get_regional_hub(market_name, type_id)
    if regional_hub:
        hub_orders = get_orders_by_type_and_location(market_name, type_id, regional_hub)
        buys = [o for o in hub_orders if o['is_buy_order']]
        sells = [o for o in hub_orders if not o['is_buy_order']]
    else:
        buys, sells = filtered_buys, filtered_sells

    highest_buy = max((o['price'] for o in buys), default=None)
    lowest_sell = min((o['price'] for o in sells), default=None)

    # Calculate total value (5pct sell price * sell volume)
    total_value = 0.0
    if sell_5pct is not None:
        # Use 5pct of filtered sells for total value
        sell_volume_for_value = sum(o['volume_remain'] for o in filtered_sells)
        total_value = sell_5pct * sell_volume_for_value / 1_000_000_000

    type_name = sde_loader.get_type_name(type_id) if sde_loader else str(type_id)

    result = {
        "name": type_name,
        "buy": round(highest_buy, 2) if highest_buy is not None else None,
        "sell": round(lowest_sell, 2) if lowest_sell is not None else None,
        "buy_5pct": round(buy_5pct, 2) if buy_5pct is not None else None,
        "sell_5pct": round(sell_5pct, 2) if sell_5pct is not None else None,
        "buy_orders": len(buys),
        "sell_orders": len(sells),
        "buy_threshold": round(buy_threshold, 2) if buy_threshold is not None else None,
        "sell_threshold": round(sell_threshold, 2) if sell_threshold is not None else None,
        "buy_volume": sum(o['volume_remain'] for o in filtered_buys),
        "sell_volume": sum(o['volume_remain'] for o in filtered_sells),
        "total_value": round(total_value, 2)
    }

    return jsonify(result), 200


# ============================================================================
# Hub Comparison Endpoint
# ============================================================================

@app.route('/v1/hub-compare/<int:type_id>', methods=['GET'])
def get_hub_comparison(type_id: int):
    """
    Get market statistics across main hubs for a type.
    """
    result = {}
    hub_data_exists = False

    for hub_key in get_market_hubs():
        if hub_key not in price_books:
            continue

        hub_location_ids = get_hub_orders_for_market(hub_key)
        if not hub_location_ids:
            continue

        # Get all orders for this type across hub locations
        all_hub_orders = []
        for location_id in hub_location_ids:
            all_hub_orders.extend(get_orders_by_type_and_location(hub_key, type_id, location_id))

        if not all_hub_orders:
            # Hub exists but no orders for this type
            result[f"{hub_key}_sell"] = None
            result[f"{hub_key}_buy"] = None
            result[f"{hub_key}_sell_volume"] = None
            result[f"{hub_key}_buy_volume"] = None
            result[f"{hub_key}_value"] = None
            result[f"{hub_key}_sell_5pct"] = None
            result[f"{hub_key}_buy_5pct"] = None
            continue

        hub_data_exists = True

        # Filter orders
        filtered_buys, filtered_sells = calculate_filtered_buy_sell(all_hub_orders)

        # Calculate prices
        highest_buy = max((o['price'] for o in filtered_buys), default=None) if filtered_buys else None
        lowest_sell = min((o['price'] for o in filtered_sells), default=None) if filtered_sells else None

        sell_5pct = calc_5pct_price(filtered_sells, False)
        buy_5pct = calc_5pct_price(filtered_buys, True)

        # Calculate value (5pct sell * sell volume, in billions)
        sell_volume = sum(o['volume_remain'] for o in filtered_sells)
        value = 0.0
        if sell_5pct is not None:
            value = sell_5pct * sell_volume / 1_000_000_000

        result[f"{hub_key}_sell"] = round(lowest_sell, 2) if lowest_sell is not None else None
        result[f"{hub_key}_buy"] = round(highest_buy, 2) if highest_buy is not None else None
        result[f"{hub_key}_sell_volume"] = sell_volume
        result[f"{hub_key}_buy_volume"] = sum(o['volume_remain'] for o in filtered_buys)
        result[f"{hub_key}_value"] = round(value, 2)
        result[f"{hub_key}_sell_5pct"] = round(sell_5pct, 2) if sell_5pct is not None else None
        result[f"{hub_key}_buy_5pct"] = round(buy_5pct, 2) if buy_5pct is not None else None

    if not hub_data_exists:
        return jsonify({
            "error": "NO_PRICE_DATA",
            "details": "No hub price data available for comparison."
        }), 404

    return jsonify(result), 200


# ============================================================================
# Main Entry Point
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description='Market Tools API')
    parser.add_argument('--port', type=int, default=5000, help='Port to listen on')
    parser.add_argument('--address', type=str, default='127.0.0.1', help='Address to bind to')
    parser.add_argument('--sde', type=str, required=True, help='Path to SDE directory')

    args = parser.parse_args()

    # Initialize SDE loader
    global sde_loader
    try:
        sde_loader = SDELoader(args.sde)
        sde_loader.load_all()
        print(f"SDE loaded successfully from {args.sde}")
    except Exception as e:
        print(f"Error loading SDE: {e}")
        return 1

    # Run Flask app
    app.run(host=args.address, port=args.port, debug=False)
    return 0


if __name__ == '__main__':
    exit(main())