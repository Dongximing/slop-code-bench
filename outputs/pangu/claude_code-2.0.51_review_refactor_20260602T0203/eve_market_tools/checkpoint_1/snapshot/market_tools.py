#!/usr/bin/env python3
"""
Market Tools API for industrialists - provides deep market tooling
including price ingestion, price books, quote queries, inventory valuation,
and depth/spread analysis across markets.
"""

import gzip
import bz2
import csv
import io
import argparse
from datetime import datetime
from collections import defaultdict
from typing import Dict, List, Optional, Any, Tuple
from flask import Flask, request, jsonify

app = Flask(__name__)


# Global state storage
class MarketState:
    def __init__(self):
        # SDE data
        self.type_names: Dict[int, str] = {}  # type_id -> name
        self.type_ids_by_name: Dict[str, int] = {}  # name -> type_id
        self.region_names: Dict[int, str] = {}  # region_id -> name
        self.region_id_map: Dict[str, int] = {}  # name -> region_id
        self.region_ids: Dict[str, int] = {}  # alias -> region_id
        self.stations: Dict[int, Dict[str, Any]] = {}  # location_id -> station data
        self.station_types: Dict[int, str] = {}  # location_id -> type (Station/Structure)
        self.station_names: Dict[int, str] = {}  # location_id -> name

        # Price books: market name -> {order_id -> order_data}
        self.price_books: Dict[str, Dict[int, Dict[str, Any]]] = defaultdict(dict)

        # Market hub mappings - main hubs by name
        self.hub_names = ['jita', 'amarr', 'dodixie', 'rens', 'hek']

        # Hub station location IDs
        self.hub_station_ids: Dict[str, List[int]] = {
            'jita': [],
            'amarr': [],
            'dodixie': [],
            'rens': [],
            'hek': [],
        }

        # Region to hub mapping
        self.region_to_hub: Dict[int, str] = {}


state = MarketState()


# =============================================================================
# SDE Loading
# =============================================================================

def load_sde(sde_path: str) -> None:
    """Load SDE files for name/ID resolution."""
    # Load region names from bz2 CSV
    _load_bz2_csv(f"{sde_path}/mapRegions.csv.bz2", _process_region)

    # Load type names from bz2 CSV
    _load_bz2_csv(f"{sde_path}/invTypes.csv.bz2", _process_type)

    # Load stations from bz2 CSV
    _load_bz2_csv(f"{sde_path}/staStations.csv.bz2", _process_station)

    # Initialize hub stations
    _initialize_hubs()


def _load_bz2_csv(filepath: str, processor) -> None:
    """Load a bz2 compressed CSV file."""
    with bz2.open(filepath, 'rt', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            processor(row)


def _process_region(row: Dict[str, str]) -> None:
    region_id = int(row['regionID'])
    region_name = row['regionName']
    state.region_names[region_id] = region_name
    state.region_id_map[region_name] = region_id

    # Create aliases
    alias = region_name.lower().replace(' ', '_').replace('-', '_')
    state.region_ids[alias] = region_id
    state.region_ids[region_name.lower().replace(' ', '_')] = region_id


def _process_type(row: Dict[str, str]) -> None:
    type_id = int(row['typeID'])
    type_name = row['typeName']
    state.type_names[type_id] = type_name
    state.type_ids_by_name[type_name] = type_id


def _process_station(row: Dict[str, str]) -> None:
    try:
        # The SDE uses stationID as locationID
        location_id = int(row['stationID'])
        station_name = row['stationName']
        # In the SDE, all entries are stations, not structures
        station_type = 'Station'
        region_id = int(row.get('regionID', 0))

        state.station_types[location_id] = station_type
        state.station_names[location_id] = station_name

        # Store full station data
        state.stations[location_id] = {
            'location_id': location_id,
            'type': station_type,
            'name': station_name,
            'solar_system_id': int(row.get('solarSystemID', 0)),
            'region_id': region_id,
        }
    except (ValueError, KeyError):
        pass


def _initialize_hubs() -> None:
    """Initialize hub station location IDs based on known hub names."""
    # Define hub identification patterns - exactly match the specified hubs
    hub_patterns = {
        'jita': [
            ['Jita', 'IV', 'Moon', '4', 'Caldari', 'Navy', 'Assembly', 'Plant'],
        ],
        'amarr': [
            ['Amarr', 'VIII', '(Oris)', 'Emperor', 'Family', 'Academy'],
        ],
        'dodixie': [
            ['Dodixie', 'IX', 'Moon', '20', 'Federation', 'Navy', 'Assembly', 'Plant'],
        ],
        'rens': [
            ['Rens', 'VI', 'Moon', '8', 'Brutor', 'Tribe', 'Treasury'],
        ],
        'hek': [
            ['Hek', 'VIII', 'Moon', '12', 'Boundless', 'Creation', 'Factory'],
        ],
    }

    for location_id, name in state.station_names.items():
        for hub, patterns in hub_patterns.items():
            for pattern in patterns:
                # Check if all pattern elements are in the name
                if all(p in name for p in pattern):
                    state.hub_station_ids[hub].append(location_id)
                    break


# =============================================================================
# Error Handling
# =============================================================================

@app.errorhandler(400)
def bad_request(error):
    return jsonify({
        'error': 'INVALID_FORMAT',
        'details': str(error.description) if error.description else 'Bad request'
    }), 400


@app.errorhandler(404)
def not_found(error):
    return jsonify({
        'error': error.description.get('error', 'NOT_FOUND'),
        'details': error.description.get('details', 'Resource not found')
    }), 404


def _error_400(details: str):
    return jsonify({
        'error': 'INVALID_FORMAT',
        'details': details
    }), 400


def _error_404(error_type: str, details: str):
    return jsonify({
        'error': error_type,
        'details': details
    }), 404


# =============================================================================
# POST /v1/prices - Price Ingestion & Book Building
# =============================================================================

@app.route('/v1/prices', methods=['POST'])
def ingest_prices():
    """Ingest gzipped CSV bytes containing market orders."""
    if not request.data:
        return _error_400('No data provided')

    # Get query parameters
    market = request.args.get('market', 'jita')
    mode = request.args.get('mode', 'replace')
    location_id_str = request.args.get('location_id')
    filter_location_id = int(location_id_str) if location_id_str else None

    # Validate mode
    if mode not in ('replace', 'append'):
        return _error_400(f"Invalid mode: {mode}. Must be 'replace' or 'append'")

    # Decompress and parse CSV
    try:
        decompressed = gzip.decompress(request.data)
        text_stream = io.StringIO(decompressed.decode('utf-8'))
        reader = csv.DictReader(text_stream)

        # Check required columns
        required_columns = [
            'order_id', 'duration', 'is_buy_order', 'issued', 'location_id',
            'min_volume', 'price', 'range', 'system_id', 'type_id',
            'volume_remain', 'volume_total'
        ]

        if not reader.fieldnames:
            return _error_400('CSV file has no headers')

        missing = set(required_columns) - set(reader.fieldnames)
        if missing:
            return _error_400(f'Missing required columns: {", ".join(sorted(missing))}')

        # Process orders
        orders_count = 0
        existing_orders = dict(state.price_books.get(market, {}))

        for row in reader:
            try:
                order_id = int(row['order_id'])
                location_id = int(row['location_id'])

                # Filter by location_id if provided
                if filter_location_id and location_id != filter_location_id:
                    continue

                # Parse is_buy_order - handle both string and boolean
                is_buy_str = row['is_buy_order']
                if isinstance(is_buy_str, str):
                    is_buy_order = is_buy_str.lower() == 'true'
                else:
                    is_buy_order = bool(is_buy_str)

                order = {
                    'order_id': order_id,
                    'duration': int(row['duration']),
                    'is_buy_order': is_buy_order,
                    'issued': row['issued'],
                    'location_id': location_id,
                    'min_volume': int(row['min_volume']),
                    'price': float(row['price']),
                    'range': row['range'],
                    'system_id': int(row['system_id']),
                    'type_id': int(row['type_id']),
                    'volume_remain': int(row['volume_remain']),
                    'volume_total': int(row['volume_total']),
                }

                # Upsert logic based on mode
                if mode == 'append':
                    if order_id in existing_orders:
                        # Keep newer issued date
                        existing = existing_orders[order_id]
                        if order['issued'] > existing['issued']:
                            existing_orders[order_id] = order
                    else:
                        existing_orders[order_id] = order
                else:  # replace
                    existing_orders[order_id] = order

                orders_count += 1

            except (ValueError, KeyError) as e:
                return _error_400(f'Invalid row data: {str(e)}')

        state.price_books[market] = existing_orders

        return jsonify({
            'status': 'PRICES_UPDATED',
            'count': orders_count
        })

    except gzip.BadGzipFile:
        return _error_400('Invalid gzip data - file is not a valid gzipped CSV')
    except UnicodeDecodeError as e:
        return _error_400(f'Decoding error: {str(e)}')


# =============================================================================
# POST /v1/stations - Station Ingestion
# =============================================================================

@app.route('/v1/stations', methods=['POST'])
def ingest_stations():
    """Ingest gzipped CSV bytes containing station data."""
    if not request.data:
        return _error_400('No data provided')

    try:
        decompressed = gzip.decompress(request.data)
        text_stream = io.StringIO(decompressed.decode('utf-8'))
        reader = csv.DictReader(text_stream)

        # Check required columns
        required_columns = ['location_id', 'type', 'name']

        if not reader.fieldnames:
            return _error_400('CSV file has no headers')

        missing = set(required_columns) - set(reader.fieldnames)
        if missing:
            return _error_400(f'Missing required columns: {", ".join(sorted(missing))}')

        rows_count = 0

        for row in reader:
            try:
                location_id = int(row['location_id'])
                station_type = row['type']
                name = row['name']

                # Validate station type
                if station_type not in ('Station', 'Structure'):
                    return _error_400(f"Invalid station type: {station_type}")

                state.station_types[location_id] = station_type
                state.station_names[location_id] = name

                state.stations[location_id] = {
                    'location_id': location_id,
                    'type': station_type,
                    'name': name,
                }

                rows_count += 1

            except (ValueError, KeyError) as e:
                return _error_400(f'Invalid row data: {str(e)}')

        return jsonify({
            'status': 'STATIONS_UPDATED',
            'count': rows_count
        })

    except gzip.BadGzipFile:
        return _error_400('Invalid gzip data - file is not a valid gzipped CSV')
    except UnicodeDecodeError as e:
        return _error_400(f'Decoding error: {str(e)}')


# =============================================================================
# Helper Functions for Market Calculations
# =============================================================================

def _filter_outliers(orders: List[Dict], is_buy: bool) -> List[Dict]:
    """Filter out outlier orders."""
    if not orders:
        return []

    if is_buy:
        # For buy orders, remove 10% highest prices (outlier filter)
        sorted_orders = sorted(orders, key=lambda x: x['price'], reverse=True)
        cutoff = max(1, int(len(sorted_orders) * 0.1))
        return sorted_orders[cutoff:]
    else:
        # For sell orders, remove orders with price > 10x lowest price
        if not orders:
            return []
        min_price = min(o['price'] for o in orders)
        return [o for o in orders if o['price'] <= min_price * 10]


def _calculate_5pct_volume_weighted_price(orders: List[Dict], is_buy: bool) -> Optional[float]:
    """
    Calculate the volume-weighted average price of the top 5% of orders
    sorted by best price.
    """
    if not orders:
        return None

    # Sort by price (descending for buy orders, ascending for sell orders)
    sorted_orders = sorted(orders, key=lambda x: x['price'], reverse=is_buy)

    # Calculate total volume
    total_volume = sum(o['volume_remain'] for o in sorted_orders)

    if total_volume == 0:
        return None

    # Calculate 5% threshold volume
    target_volume = total_volume * 0.05

    # Accumulate volume until we reach 5%
    accumulated = 0
    total_value = 0.0

    for order in sorted_orders:
        vol = order['volume_remain']
        price = order['price']

        if accumulated + vol >= target_volume:
            # Take only what we need from this order
            needed = target_volume - accumulated
            total_value += needed * price
            accumulated += needed
            break
        else:
            total_value += vol * price
            accumulated += vol

    if accumulated == 0:
        return None

    return total_value / accumulated


def _get_orders_for_region(region_id: int, type_id_filter: Optional[int] = None) -> List[Dict]:
    """Get all orders for a specific region, optionally filtered by type."""
    all_orders = []

    for market, orders in state.price_books.items():
        for order in orders.values():
            # Check if order location is in the region
            loc_id = order['location_id']
            if loc_id in state.stations:
                # Include all orders from stations in the region
                if type_id_filter is None or order['type_id'] == type_id_filter:
                    all_orders.append(order)

    return all_orders


def _determine_regional_hub(orders: List[Dict]) -> Optional[str]:
    """
    Determine the regional hub (location with most sell orders).
    Returns the hub name.
    """
    # Count sell orders by hub
    hub_order_counts = defaultdict(int)

    for order in orders:
        if not order['is_buy_order']:  # Sell orders only
            loc_id = order['location_id']
            for hub, hub_locs in state.hub_station_ids.items():
                if loc_id in hub_locs:
                    hub_order_counts[hub] += 1
                    break

    if not hub_order_counts:
        return None

    # Return hub with most sell orders
    return max(hub_order_counts.items(), key=lambda x: x[1])[0]


def _get_station_name(loc_id: int) -> str:
    """Get station name, or return location_id as string if unknown."""
    if loc_id in state.station_names:
        return state.station_names[loc_id]
    return str(loc_id)


# =============================================================================
# GET /v1/market/{regionID} - Market Stats
# =============================================================================

@app.route('/v1/market/<region_identifier>', methods=['GET'])
def get_market_stats(region_identifier: str):
    """Get market statistics for a region."""
    # Resolve region ID
    region_id_int = None

    try:
        region_id_int = int(region_identifier)
    except ValueError:
        # Try to resolve by name/alias
        region_key = region_identifier.lower().replace(' ', '_').replace('-', '_')
        if region_key in state.region_ids:
            region_id_int = state.region_ids[region_key]

    if region_id_int is None or region_id_int not in state.region_names:
        return _error_404('UNKNOWN_ITEMS', f'Unknown region: {region_identifier}')

    region_name = state.region_names[region_id_int]

    # Get query parameters
    type_ids_str = request.args.get('type_ids')
    include_hubs = request.args.get('hubs') is not None

    # Parse type IDs if provided
    type_ids = []
    if type_ids_str:
        try:
            type_ids = [int(tid.strip()) for tid in type_ids_str.split(',')]
        except ValueError:
            return _error_400('Invalid type_ids parameter')

    # Get all orders for this region
    region_orders = _get_orders_for_region(region_id_int)

    if not region_orders:
        # Return empty stats
        return jsonify({
            'name': region_name,
            'sell_orders': 0,
            'buy_orders': 0,
            'sell_value': 0.0,
            'buy_value': 0.0,
            'unique_items': 0,
        })

    # Determine regional hub
    regional_hub = _determine_regional_hub(region_orders)

    # Get hub orders for calculations (use regional hub if identified)
    hub_orders = []
    if regional_hub:
        hub_locs = state.hub_station_ids.get(regional_hub, [])
        for order in region_orders:
            if order['location_id'] in hub_locs:
                hub_orders.append(order)
    else:
        # Fallback: use all region orders
        hub_orders = region_orders

    # Separate buy/sell orders
    sell_orders = [o for o in hub_orders if not o['is_buy_order']]
    buy_orders = [o for o in hub_orders if o['is_buy_order']]

    # Filter outliers
    sell_orders_filtered = _filter_outliers(sell_orders, is_buy=False)
    buy_orders_filtered = _filter_outliers(buy_orders, is_buy=True)

    # Calculate values (in billions, rounded to 2 decimal places)
    sell_value = sum(o['price'] * o['volume_remain'] for o in sell_orders_filtered) / 1e9
    buy_value = sum(o['price'] * o['volume_remain'] for o in buy_orders_filtered) / 1e9

    # Unique items
    unique_items = len(set(o['type_id'] for o in region_orders))

    result = {
        'name': region_name,
        'sell_orders': len(sell_orders_filtered),
        'buy_orders': len(buy_orders_filtered),
        'sell_value': round(sell_value, 2),
        'buy_value': round(buy_value, 2),
        'unique_items': unique_items,
    }

    # Add type details if type_ids provided
    if type_ids:
        result['types'] = {}
        for tid in type_ids:
            type_info = _get_type_stats(tid, hub_orders)
            if type_info:
                type_name = state.type_names.get(tid, str(tid))
                result['types'][type_name] = type_info

    # Add hubs if requested
    if include_hubs:
        result['hubs'] = _get_region_hubs_stats(region_id_int, region_orders)

    return jsonify(result)


def _get_type_stats(type_id: int, orders: List[Dict]) -> Optional[Dict]:
    """Get statistics for a specific type from a list of orders."""
    type_orders = [o for o in orders if o['type_id'] == type_id]

    if not type_orders:
        return None

    buy_orders = [o for o in type_orders if o['is_buy_order']]
    sell_orders = [o for o in type_orders if not o['is_buy_order']]

    # Filter outliers
    buy_orders = _filter_outliers(buy_orders, is_buy=True)
    sell_orders = _filter_outliers(sell_orders, is_buy=False)

    # Best prices
    best_buy = max(buy_orders, key=lambda x: x['price']) if buy_orders else None
    best_sell = min(sell_orders, key=lambda x: x['price']) if sell_orders else None

    split = (best_buy['price'] + best_sell['price']) / 2 if best_buy and best_sell else None

    return {
        'buy': best_buy['price'] if best_buy else None,
        'sell': best_sell['price'] if best_sell else None,
        'split': split,
        'buy_orders': len(buy_orders),
        'sell_orders': len(sell_orders),
    }


def _get_region_hubs_stats(region_id: int, region_orders: List[Dict]) -> List[Dict]:
    """Get hub statistics for a region."""
    hubs_data = []

    for hub_name in state.hub_names:
        hub_locs = state.hub_station_ids.get(hub_name, [])

        # Filter orders for this hub
        hub_orders = [o for o in region_orders if o['location_id'] in hub_locs]

        if not hub_orders:
            continue

        # Get station name for the hub
        station_name = None
        for loc_id in hub_locs:
            if loc_id in state.station_names:
                station_name = state.station_names[loc_id]
                break

        if not station_name:
            # If no known station name, use the first location_id as string
            if hub_locs:
                station_name = str(hub_locs[0])
            else:
                station_name = hub_name

        # Count sell orders and calculate sell value (in billions)
        sell_orders = [o for o in hub_orders if not o['is_buy_order']]
        sell_value = sum(o['price'] * o['volume_remain'] for o in sell_orders) / 1e9

        hubs_data.append({
            'station': station_name,
            'orders': len(sell_orders),
            'sell_value': round(sell_value, 2),
        })

    # Sort by number of sell orders (descending)
    hubs_data.sort(key=lambda x: x['orders'], reverse=True)

    return hubs_data


# =============================================================================
# GET /v1/market/{regionID}/{typeID} - Detailed Type Stats
# =============================================================================

@app.route('/v1/market/<region_identifier>/<type_identifier>', methods=['GET'])
def get_type_market_stats(region_identifier: str, type_identifier: str):
    """Get detailed market statistics for a specific type in a region."""
    # Resolve region ID
    region_id_int = None

    try:
        region_id_int = int(region_identifier)
    except ValueError:
        region_key = region_identifier.lower().replace(' ', '_').replace('-', '_')
        if region_key in state.region_ids:
            region_id_int = state.region_ids[region_key]

    if region_id_int is None or region_id_int not in state.region_names:
        return _error_404('UNKNOWN_ITEMS', f'Unknown region: {region_identifier}')

    # Resolve type ID
    try:
        type_id_int = int(type_identifier)
    except ValueError:
        return _error_400('Invalid type_id')

    # Get type name
    type_name = state.type_names.get(type_id_int)
    if not type_name:
        return _error_404('UNKNOWN_ITEMS', f'Unknown type ID: {type_identifier}')

    # Get all orders for this type in this region
    region_orders = _get_orders_for_region(region_id_int, type_id_int)

    # Determine hub and get hub orders
    regional_hub = _determine_regional_hub(region_orders)
    hub_orders = []

    if regional_hub:
        hub_locs = state.hub_station_ids.get(regional_hub, [])
        hub_orders = [o for o in region_orders if o['location_id'] in hub_locs]
    else:
        hub_orders = region_orders

    # Separate buy/sell orders
    buy_orders = [o for o in hub_orders if o['is_buy_order']]
    sell_orders = [o for o in hub_orders if not o['is_buy_order']]

    # Filter outliers
    buy_orders_filtered = _filter_outliers(buy_orders, is_buy=True)
    sell_orders_filtered = _filter_outliers(sell_orders, is_buy=False)

    # Best prices
    best_buy = max(buy_orders_filtered, key=lambda x: x['price']) if buy_orders_filtered else None
    best_sell = min(sell_orders_filtered, key=lambda x: x['price']) if sell_orders_filtered else None

    # Calculate 5% volume weighted average price
    buy_5pct = _calculate_5pct_volume_weighted_price(buy_orders_filtered, is_buy=True)
    sell_5pct = _calculate_5pct_volume_weighted_price(sell_orders_filtered, is_buy=False)

    # Thresholds (10% cutoff points)
    buy_threshold = None
    if len(buy_orders_filtered) > 0:
        sorted_buys = sorted(buy_orders_filtered, key=lambda x: x['price'], reverse=True)
        tenth_idx = max(1, int(len(sorted_buys) * 0.1))
        buy_threshold = sorted_buys[tenth_idx - 1]['price']

    sell_threshold = None
    if len(sell_orders_filtered) > 0:
        sorted_sells = sorted(sell_orders_filtered, key=lambda x: x['price'])
        tenth_idx = max(1, int(len(sorted_sells) * 0.1))
        sell_threshold = sorted_sells[tenth_idx - 1]['price']

    # Volumes
    buy_volume = sum(o['volume_remain'] for o in buy_orders_filtered)
    sell_volume = sum(o['volume_remain'] for o in sell_orders_filtered)

    # Total value (5pct sell price * sell volume, in billions)
    total_value = 0.0
    if sell_5pct is not None and sell_volume > 0:
        total_value = (sell_5pct * sell_volume) / 1e9

    return jsonify({
        'name': type_name,
        'buy': best_buy['price'] if best_buy else None,
        'sell': best_sell['price'] if best_sell else None,
        'buy_5pct': buy_5pct,
        'sell_5pct': sell_5pct,
        'buy_orders': len(buy_orders_filtered),
        'sell_orders': len(sell_orders_filtered),
        'buy_threshold': buy_threshold,
        'sell_threshold': sell_threshold,
        'buy_volume': buy_volume,
        'sell_volume': sell_volume,
        'total_value': round(total_value, 2),
    })


# =============================================================================
# GET /v1/hub-compare/{typeID} - Hub Comparison
# =============================================================================

@app.route('/v1/hub-compare/<type_identifier>', methods=['GET'])
def hub_compare(type_identifier: str):
    """Get market statistics across the main hubs for a type."""
    try:
        type_id_int = int(type_identifier)
    except ValueError:
        return _error_400('Invalid type_id')

    # Get type name
    type_name = state.type_names.get(type_id_int)
    if not type_name:
        return _error_404('UNKNOWN_ITEMS', f'Unknown type ID: {type_identifier}')

    result = {}
    has_data = False

    for hub_name in state.hub_names:
        hub_stats = _get_hub_stats_for_type(hub_name, type_id_int)
        if hub_stats:
            has_data = True
            result[f'{hub_name}_sell'] = hub_stats['sell']
            result[f'{hub_name}_buy'] = hub_stats['buy']
            result[f'{hub_name}_sell_volume'] = hub_stats['sell_volume']
            result[f'{hub_name}_buy_volume'] = hub_stats['buy_volume']
            result[f'{hub_name}_value'] = hub_stats['value']
            result[f'{hub_name}_sell_5pct'] = hub_stats['sell_5pct']
            result[f'{hub_name}_buy_5pct'] = hub_stats['buy_5pct']

    if not has_data:
        return _error_404('NO_PRICE_DATA', 'No price data available for any hub')

    return jsonify(result)


def _get_hub_stats_for_type(hub_name: str, type_id: int) -> Optional[Dict]:
    """Get market stats for a specific hub and type."""
    location_ids = state.hub_station_ids.get(hub_name, [])
    if not location_ids:
        return None

    # Get all orders for this type at these locations
    hub_orders = []
    for market, orders in state.price_books.items():
        for order in orders.values():
            if order['type_id'] == type_id and order['location_id'] in location_ids:
                hub_orders.append(order)

    if not hub_orders:
        return None

    buy_orders = [o for o in hub_orders if o['is_buy_order']]
    sell_orders = [o for o in hub_orders if not o['is_buy_order']]

    # Apply outlier filtering
    buy_orders_filtered = _filter_outliers(buy_orders, is_buy=True)
    sell_orders_filtered = _filter_outliers(sell_orders, is_buy=False)

    # Best prices
    best_buy = max(buy_orders_filtered, key=lambda x: x['price']) if buy_orders_filtered else None
    best_sell = min(sell_orders_filtered, key=lambda x: x['price']) if sell_orders_filtered else None

    # 5% volume weighted prices
    buy_5pct = _calculate_5pct_volume_weighted_price(buy_orders_filtered, is_buy=True)
    sell_5pct = _calculate_5pct_volume_weighted_price(sell_orders_filtered, is_buy=False)

    # Volumes (post filtering)
    buy_volume = sum(o['volume_remain'] for o in buy_orders_filtered)
    sell_volume = sum(o['volume_remain'] for o in sell_orders_filtered)

    # Value (5pct sell price * sell volume, in billions)
    value = 0.0
    if sell_5pct is not None and sell_volume > 0:
        value = (sell_5pct * sell_volume) / 1e9

    return {
        'sell': best_sell['price'] if best_sell else None,
        'buy': best_buy['price'] if best_buy else None,
        'sell_volume': buy_volume,
        'buy_volume': sell_volume,
        'value': round(value, 2),
        'sell_5pct': sell_5pct,
        'buy_5pct': buy_5pct,
    }


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description='Market Tools API')
    parser.add_argument('--port', type=int, default=5000, help='Port to listen on')
    parser.add_argument('--address', type=str, default='127.0.0.1', help='Address to bind to')
    parser.add_argument('--sde', type=str, required=True, help='Path to SDE directory')

    args = parser.parse_args()

    # Load SDE
    print(f"Loading SDE from {args.sde}...")
    load_sde(args.sde)
    print(f"Loaded {len(state.type_names)} types, {len(state.region_names)} regions, {len(state.stations)} stations")
    print(f"Hub stations: Jita={len(state.hub_station_ids['jita'])}, Amarr={len(state.hub_station_ids['amarr'])}, Dodixie={len(state.hub_station_ids['dodixie'])}, Rens={len(state.hub_station_ids['rens'])}, Hek={len(state.hub_station_ids['hek'])}")

    # Start Flask app
    app.run(host=args.address, port=args.port, debug=False)


if __name__ == '__main__':
    main()
