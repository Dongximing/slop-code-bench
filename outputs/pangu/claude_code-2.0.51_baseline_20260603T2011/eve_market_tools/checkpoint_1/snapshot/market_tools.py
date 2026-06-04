#!/usr/bin/env python3
"""
Market Tools API - Industrialists' market data and price book management.
"""

import gzip
import csv
import io
import os
import argparse
from datetime import datetime
from collections import defaultdict
from typing import Dict, List, Optional, Any, Tuple
from flask import Flask, request, jsonify
from flask_restful import Api, Resource

app = Flask(__name__)
api = Api(app)

# Data storage - will be initialized with SDE
price_data: Dict[str, Dict[int, Dict[int, Dict]]] = defaultdict(lambda: defaultdict(dict))  # market -> type_id -> order_id -> order
stations: Dict[int, Dict] = {}  # location_id -> station info
name_to_id: Dict[str, int] = {}
id_to_name: Dict[int, str] = {}

# Main hub stations (by location_id)
MAIN_HUBS = {
    "jita": 60003761,   # Jita IV - Moon 4 - Caldari Navy Assembly Plant
    "amarr": 60008494,  # Amarr VIII (Oris) - Emperor Family Academy
    "dodixie": 60014718, # Dodixie IX - Moon 20 - Federation Navy Assembly Plant
    "rens": 60004588,   # Rens VI - Moon 8 - Brutor Tribe Treasury
    "hek": 60005614,    # Hek VIII - Moon 12 - Boundless Creation Factory
}

# Region mappings (simplified - would come from SDE)
REGIONS = {
    10000002: "The Forge",     # Jita region
    10000043: "Domain",        # Amarr region
    10000032: "Sinq Laison",   # Dodixie region
    10000030: "Verge Vendor",  # Rens region
    10000031: "Essence",       # Hek region
}


class SDELoader:
    """Handles loading and querying the Static Data Export."""

    def __init__(self, sde_path: str):
        self.sde_path = sde_path
        self.load_sde()

    def load_sde(self):
        """Load all SDE data from compressed CSV files."""
        # Load invTypes for name/ID resolution
        types_file = os.path.join(self.sde_path, "invTypes.csv.bz2")
        if os.path.exists(types_file):
            self._load_types(types_file)

        # Load stations
        stations_file = os.path.join(self.sde_path, "staStations.csv.bz2")
        if os.path.exists(stations_file):
            self._load_stations(stations_file)

    def _load_types(self, filepath: str):
        """Load type names and IDs from invTypes.csv."""
        try:
            import bz2
            with bz2.open(filepath, 'rt', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    type_id = int(row['typeID'])
                    name = row.get('typeName', '')
                    if name:
                        id_to_name[type_id] = name
                        name_to_id[name] = type_id
        except Exception as e:
            raise ValueError(f"Error loading types file: {e}")

    def _load_stations(self, filepath: str):
        """Load station data from staStations.csv."""
        try:
            import bz2
            with bz2.open(filepath, 'rt', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    location_id = int(row['stationID'])
                    if location_id not in stations:
                        stations[location_id] = {
                            'location_id': location_id,
                            'type': 'Station',
                            'name': row.get('stationName', ''),
                            'system_id': int(row.get('solarSystemID', 0)),
                            'region_id': int(row.get('regionID', 0))
                        }
        except Exception as e:
            raise ValueError(f"Error loading stations file: {e}")

    def get_type_name(self, type_id: int) -> Optional[str]:
        """Get type name from ID."""
        return id_to_name.get(type_id)

    def get_type_id(self, name: str) -> Optional[int]:
        """Get type ID from name."""
        return name_to_id.get(name)

    def get_region_name(self, region_id: int) -> str:
        """Get region name from ID."""
        return REGIONS.get(region_id, f"Unknown Region {region_id}")


class PriceIngestion(Resource):
    """Handle price data ingestion from gzipped CSV."""

    def post(self):
        try:
            # Get raw gzipped data
            if not request.data:
                return {"error": "INVALID_FORMAT", "details": "No data provided"}, 400

            # Decompress gzipped data
            try:
                decompressed = gzip.decompress(request.data)
            except Exception as e:
                return {"error": "INVALID_FORMAT", "details": f"Decompression error: {e}"}, 400

            # Parse CSV
            try:
                csv_data = csv.DictReader(io.StringIO(decompressed.decode('utf-8')))
            except Exception as e:
                return {"error": "INVALID_FORMAT", "details": f"CSV parsing error: {e}"}, 400

            # Validate required columns
            required_columns = [
                'order_id', 'duration', 'is_buy_order', 'issued', 'location_id',
                'min_volume', 'price', 'range', 'system_id', 'type_id',
                'volume_remain', 'volume_total'
            ]
            if not csv_data.fieldnames:
                return {"error": "INVALID_FORMAT", "details": "Empty CSV file"}, 400

            missing = [col for col in required_columns if col not in csv_data.fieldnames]
            if missing:
                return {"error": "INVALID_FORMAT", "details": f"Missing required columns: {', '.join(missing)}"}, 400

            # Get query parameters
            market = request.args.get('market', 'jita')
            mode = request.args.get('mode', 'replace')
            location_id_override = request.args.get('location_id')
            if location_id_override:
                try:
                    location_id_override = int(location_id_override)
                except ValueError:
                    return {"error": "INVALID_FORMAT", "details": "Invalid location_id"}, 400

            # Process orders - group by type_id
            new_orders_by_type = defaultdict(dict)

            for row in csv_data:
                try:
                    order = {
                        'order_id': int(row['order_id']),
                        'duration': int(row['duration']),
                        'is_buy_order': row['is_buy_order'].lower() in ('true', '1', 'yes'),
                        'issued': row['issued'],
                        'location_id': int(row['location_id']),
                        'min_volume': int(row['min_volume']),
                        'price': float(row['price']),
                        'range': row['range'],
                        'system_id': int(row['system_id']),
                        'type_id': int(row['type_id']),
                        'volume_remain': int(row['volume_remain']),
                        'volume_total': int(row['volume_total']),
                    }

                    type_id = order['type_id']

                    # Apply location filter if specified
                    if location_id_override and order['location_id'] != location_id_override:
                        continue

                    # For append mode, handle upsert by order_id within each type
                    if mode == 'append':
                        existing = new_orders_by_type[type_id].get(order['order_id'])
                        if existing is None or order['issued'] > existing['issued']:
                            new_orders_by_type[type_id][order['order_id']] = order
                    else:
                        new_orders_by_type[type_id][order['order_id']] = order

                except (ValueError, KeyError) as e:
                    return {"error": "INVALID_FORMAT", "details": f"Invalid data in row: {e}"}, 400

            rows_processed = sum(len(orders) for orders in new_orders_by_type.values())

            # Store in price_data
            if mode == 'replace':
                # Replace mode - clear existing data
                price_data[market] = {}

            # Merge or replace data
            existing_market = price_data[market]
            for type_id, orders_by_id in new_orders_by_type.items():
                if mode == 'append':
                    # Merge with existing orders
                    existing_type_orders = existing_market.get(type_id, {})
                    merged = {**existing_type_orders, **orders_by_id}
                    # Handle upsert conflicts where newer issued wins
                    for order_id, new_order in orders_by_id.items():
                        if order_id in existing_type_orders:
                            existing_order = existing_type_orders[order_id]
                            if existing_order['issued'] > new_order['issued']:
                                merged[order_id] = existing_order
                    existing_market[type_id] = merged
                else:
                    existing_market[type_id] = orders_by_id

            return {"status": "PRICES_UPDATED", "count": rows_processed}, 200

        except Exception as e:
            return {"error": "INVALID_FORMAT", "details": str(e)}, 400


class StationIngestion(Resource):
    """Handle station data ingestion from gzipped CSV."""

    def post(self):
        try:
            if not request.data:
                return {"error": "INVALID_FORMAT", "details": "No data provided"}, 400

            try:
                decompressed = gzip.decompress(request.data)
            except Exception as e:
                return {"error": "INVALID_FORMAT", "details": f"Decompression error: {e}"}, 400

            try:
                csv_data = csv.DictReader(io.StringIO(decompressed.decode('utf-8')))
            except Exception as e:
                return {"error": "INVALID_FORMAT", "details": f"CSV parsing error: {e}"}, 400

            required_columns = ['location_id', 'type', 'name']
            if not csv_data.fieldnames:
                return {"error": "INVALID_FORMAT", "details": "Empty CSV file"}, 400

            missing = [col for col in required_columns if col not in csv_data.fieldnames]
            if missing:
                return {"error": "INVALID_FORMAT", "details": f"Missing required columns: {', '.join(missing)}"}, 400

            rows_processed = 0
            for row in csv_data:
                try:
                    location_id = int(row['location_id'])
                    station_type = row['type']
                    name = row['name']

                    station = {
                        'location_id': location_id,
                        'type': station_type,
                        'name': name,
                        'system_id': int(row.get('system_id', 0)),
                        'region_id': int(row.get('region_id', 0))
                    }

                    stations[location_id] = station
                    rows_processed += 1

                except (ValueError, KeyError) as e:
                    return {"error": "INVALID_FORMAT", "details": f"Invalid data in row: {e}"}, 400

            return {"status": "STATIONS_UPDATED", "count": rows_processed}, 200

        except Exception as e:
            return {"error": "INVALID_FORMAT", "details": str(e)}, 400


class MarketRegion(Resource):
    """Handle market statistics for a region."""

    def get(self, regionID):
        """Get market stats for a region."""
        type_ids_param = request.args.get('type_ids')
        hubs_param = request.args.get('hubs')

        type_ids = []
        if type_ids_param:
            type_ids = [int(tid.strip()) for tid in type_ids_param.split(',') if tid.strip()]

        include_hubs = hubs_param is not None

        # Find the hub with most sell orders
        hub_orders = defaultdict(lambda: {'sell': 0, 'buy': 0, 'orders': []})
        region_name = sde_loader.get_region_name(int(regionID))

        # Collect all orders in the region
        all_orders = []
        for market, types_dict in price_data.items():
            for type_id, order_dict in types_dict.items():
                all_orders.extend(order_dict.values())

        if not all_orders and not type_ids:
            return {
                "name": region_name,
                "sell_orders": 0,
                "buy_orders": 0,
                "sell_value": 0.0,
                "buy_value": 0.0,
                "unique_items": 0
            }, 200

        # Calculate stats
        sell_orders = 0
        buy_orders = 0
        sell_value = 0.0
        buy_value = 0.0
        unique_items = set()
        types_data = {}

        for type_id in type_ids:
            type_name = sde_loader.get_type_name(type_id) or f"Unknown {type_id}"
            orders = []

            # Collect orders for this type
            for market, orders_dict in price_data.items():
                type_orders = orders_dict.get(type_id, {})
                orders.extend(type_orders.values())

            buy_list = [o for o in orders if o['is_buy_order']]
            sell_list = [o for o in orders if not o['is_buy_order']]

            if buy_list or sell_list:
                unique_items.add(type_id)

            if buy_list:
                highest_buy = max(o['price'] for o in buy_list)
                outlier_threshold = highest_buy * 1.1
                filtered_buy = [o for o in buy_list if o['price'] >= outlier_threshold]
                if filtered_buy:
                    highest_buy = max(o['price'] for o in filtered_buy)
                else:
                    filtered_buy = buy_list

                buy_orders += len(filtered_buy)
                buy_value += sum(o['price'] * o['volume_remain'] for o in filtered_buy)

            if sell_list:
                lowest_sell = min(o['price'] for o in sell_list)
                outlier_threshold = lowest_sell * 10
                filtered_sell = [o for o in sell_list if o['price'] <= outlier_threshold]
                if filtered_sell:
                    lowest_sell = min(o['price'] for o in filtered_sell)
                else:
                    filtered_sell = sell_list

                sell_orders += len(filtered_sell)
                sell_value += sum(o['price'] * o['volume_remain'] for o in filtered_sell)

            if type_ids and (buy_list or sell_list):
                buy_price = None
                sell_price = None

                if filtered_buy:
                    buy_price = max(o['price'] for o in filtered_buy)
                if filtered_sell:
                    sell_price = min(o['price'] for o in filtered_sell)

                split = None
                if buy_price is not None and sell_price is not None:
                    split = (buy_price + sell_price) / 2

                types_data[type_name] = {
                    "buy": buy_price,
                    "sell": sell_price,
                    "split": split,
                    "buy_orders": len(filtered_buy),
                    "sell_orders": len(filtered_sell)
                }

        response = {
            "name": region_name,
            "sell_orders": sell_orders,
            "buy_orders": buy_orders,
            "sell_value": round(sell_value / 1_000_000_000, 2),
            "buy_value": round(buy_value / 1_000_000_000, 2),
            "unique_items": len(unique_items) if type_ids else 0
        }

        if type_ids:
            response['types'] = types_data

        if include_hubs:
            hub_list = self._get_hub_stats(int(regionID))
            response['hubs'] = hub_list

        return response, 200

    def _get_hub_stats(self, region_id: int):
        """Get hub statistics for a region."""
        hubs_data = []

        for hub_name, hub_location in MAIN_HUBS.items():
            orders = []
            for market, orders_dict in price_data.items():
                for type_id, order_dict in orders_dict.items():
                    for order_id, order in order_dict.items():
                        if order['location_id'] == hub_location:
                            orders.append(order)

            if not orders:
                continue

            sell_orders = [o for o in orders if not o['is_buy_order']]
            buy_orders = [o for o in orders if o['is_buy_order']]

            sell_value = sum(o['price'] * o['volume_remain'] for o in sell_orders)
            sell_value_billions = round(sell_value / 1_000_000_000, 2)

            # Get station name
            station_name = stations.get(hub_location, {}).get('name', str(hub_location))

            hubs_data.append({
                "station": station_name,
                "orders": len(sell_orders),
                "sell_value": sell_value_billions
            })

        # Sort by number of sell orders
        hubs_data.sort(key=lambda x: x['orders'], reverse=True)
        return hubs_data


class MarketType(Resource):
    """Handle market statistics for a specific type in a region."""

    def get(self, regionID, typeID):
        """Get detailed market stats for a specific type in a region."""
        type_id = int(typeID)
        # Collect orders for this type
        orders = []
        for market, orders_dict in price_data.items():
            type_orders = orders_dict.get(type_id, {})
            orders.extend(type_orders.values())

        if not orders:
            type_name = sde_loader.get_type_name(int(typeID)) or f"Unknown {typeID}"
            return {
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
            }, 200

        buy_list = [o for o in orders if o['is_buy_order']]
        sell_list = [o for o in orders if not o['is_buy_order']]

        # Filter outliers
        buy_threshold = None
        sell_threshold = None

        if buy_list:
            highest_buy = max(o['price'] for o in buy_list)
            buy_threshold = highest_buy * 0.9  # 10% outlier
            buy_list = [o for o in buy_list if o['price'] >= buy_threshold]

        if sell_list:
            lowest_sell = min(o['price'] for o in sell_list)
            sell_threshold = lowest_sell * 10  # 10x outlier
            sell_list = [o for o in sell_list if o['price'] <= sell_threshold]

        # Calculate 5pct volume-weighted average
        buy_5pct = self._calculate_5pct_price(buy_list, reverse=True)  # Sort descending for buys
        sell_5pct = self._calculate_5pct_price(sell_list, reverse=False)  # Sort ascending for sells

        # Final prices
        buy_price = None
        sell_price = None
        if buy_list:
            buy_price = max(o['price'] for o in buy_list)
        if sell_list:
            sell_price = min(o['price'] for o in sell_list)

        type_name = sde_loader.get_type_name(int(typeID)) or f"Unknown {typeID}"

        # Total value using 5pct sell price
        total_value = 0.0
        if sell_5pct is not None:
            total_value = sum(o['volume_remain'] for o in sell_list) * sell_5pct

        return {
            "name": type_name,
            "buy": buy_price,
            "sell": sell_price,
            "buy_5pct": buy_5pct,
            "sell_5pct": sell_5pct,
            "buy_orders": len(buy_list),
            "sell_orders": len(sell_list),
            "buy_threshold": buy_threshold,
            "sell_threshold": sell_threshold,
            "buy_volume": sum(o['volume_remain'] for o in buy_list),
            "sell_volume": sum(o['volume_remain'] for o in sell_list),
            "total_value": round(total_value, 2)
        }, 200

    def _calculate_5pct_price(self, orders: List[Dict], reverse: bool) -> Optional[float]:
        """Calculate volume-weighted average price of top 5% of orders."""
        if not orders:
            return None

        # Sort by price
        sorted_orders = sorted(orders, key=lambda o: o['price'], reverse=reverse)

        total_volume = sum(o['volume_remain'] for o in sorted_orders)
        if total_volume == 0:
            return None

        target_volume = total_volume * 0.05
        accumulated = 0
        total_value = 0.0

        for order in sorted_orders:
            remaining = target_volume - accumulated
            if remaining <= 0:
                break

            take = min(order['volume_remain'], remaining)
            total_value += take * order['price']
            accumulated += take

        return round(total_value / accumulated, 2) if accumulated > 0 else None


class HubCompare(Resource):
    """Compare market statistics across main hubs."""

    def get(self, typeID):
        """Get market stats for a type across all main hubs."""
        type_id = int(typeID)
        result = {}
        has_data = False

        for hub_name, hub_location in MAIN_HUBS.items():
            orders = []
            for market, orders_dict in price_data.items():
                type_orders = orders_dict.get(type_id, {})
                for order_id, order in type_orders.items():
                    if order['location_id'] == hub_location:
                        orders.append(order)

            if not orders:
                continue

            has_data = True
            buy_list = [o for o in orders if o['is_buy_order']]
            sell_list = [o for o in orders if not o['is_buy_order']]

            # Filter outliers
            if buy_list:
                highest_buy = max(o['price'] for o in buy_list)
                buy_threshold = highest_buy * 0.9
                buy_list = [o for o in buy_list if o['price'] >= buy_threshold]

            if sell_list:
                lowest_sell = min(o['price'] for o in sell_list)
                sell_threshold = lowest_sell * 10
                sell_list = [o for o in sell_list if o['price'] <= sell_threshold]

            # Calculate prices
            buy_price = max(o['price'] for o in buy_list) if buy_list else None
            sell_price = min(o['price'] for o in sell_list) if sell_list else None

            buy_volume = sum(o['volume_remain'] for o in buy_list) if buy_list else 0
            sell_volume = sum(o['volume_remain'] for o in sell_list) if sell_list else 0

            # Calculate 5pct prices
            buy_5pct = self._calculate_5pct(buy_list, reverse=True) if buy_list else None
            sell_5pct = self._calculate_5pct(sell_list, reverse=False) if sell_list else None

            # Calculate value (5pct sell * sell volume in billions)
            value = 0.0
            if sell_5pct and sell_volume > 0:
                value = round((sell_5pct * sell_volume) / 1_000_000_000, 2)

            result[f"{hub_name}_buy"] = buy_price
            result[f"{hub_name}_sell"] = sell_price
            result[f"{hub_name}_sell_volume"] = float(sell_volume)
            result[f"{hub_name}_buy_volume"] = float(buy_volume)
            result[f"{hub_name}_value"] = value if value > 0 else 0.0
            result[f"{hub_name}_sell_5pct"] = sell_5pct
            result[f"{hub_name}_buy_5pct"] = buy_5pct

        if not has_data:
            return {"error": "NO_PRICE_DATA", "details": f"No price data found for type {typeID} in any hub"}, 404

        return result, 200

    def _calculate_5pct(self, orders: List[Dict], reverse: bool) -> Optional[float]:
        """Calculate volume-weighted average price of top 5% of orders."""
        if not orders:
            return None

        sorted_orders = sorted(orders, key=lambda o: o['price'], reverse=reverse)
        total_volume = sum(o['volume_remain'] for o in sorted_orders)

        if total_volume == 0:
            return None

        target_volume = total_volume * 0.05
        accumulated = 0
        total_value = 0.0

        for order in sorted_orders:
            remaining = target_volume - accumulated
            if remaining <= 0:
                break
            take = min(order['volume_remain'], remaining)
            total_value += take * order['price']
            accumulated += take

        return round(total_value / accumulated, 2) if accumulated > 0 else None


class HealthCheck(Resource):
    """Simple health check endpoint."""

    def get(self):
        return {"status": "healthy"}, 200


# Global SDE loader
sde_loader = None


def main():
    global sde_loader

    parser = argparse.ArgumentParser(description='Market Tools API')
    parser.add_argument('--port', type=int, default=5000, help='Port to listen on')
    parser.add_argument('--address', type=str, default='127.0.0.1', help='Address to listen on')
    parser.add_argument('--sde', type=str, required=True, help='Path to SDE directory')

    args = parser.parse_args()

    # Verify SDE path
    if not os.path.isdir(args.sde):
        print(f"Error: SDE directory not found at {args.sde}")
        return 1

    # Load SDE
    print(f"Loading SDE from {args.sde}...")
    sde_loader = SDELoader(args.sde)
    print(f"SDE loaded: {len(id_to_name)} type names, {len(stations)} stations")

    # Register API resources
    api.add_resource(PriceIngestion, '/v1/prices')
    api.add_resource(StationIngestion, '/v1/stations')
    api.add_resource(MarketRegion, '/v1/market/<regionID>')
    api.add_resource(MarketType, '/v1/market/<regionID>/<typeID>')
    api.add_resource(HubCompare, '/v1/hub-compare/<typeID>')
    api.add_resource(HealthCheck, '/health')

    # Run Flask app
    app.run(host=args.address, port=args.port, debug=False)
    return 0


if __name__ == '__main__':
    exit(main())
