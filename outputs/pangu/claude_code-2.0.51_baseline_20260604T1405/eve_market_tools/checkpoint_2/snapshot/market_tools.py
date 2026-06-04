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
class TypeInfo:
    """Extended type information for reprocessing."""
    id: int
    name: str
    group_id: int
    category_id: int
    volume: float
    mass: float
    portion_size: int
    base_price: float | None
    published: int
    materials: list[tuple[int, int]] = field(default_factory=list)  # (material_type_id, quantity)
    attributes: dict[int, float] = field(default_factory=dict)  # attribute_id -> value


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
    types: dict[int, TypeInfo] = field(default_factory=dict)
    groups: dict[int, int] = field(default_factory=dict)  # group_id -> category_id
    categories: dict[int, str] = field(default_factory=dict)  # category_id -> name
    loaded: bool = False

    def load(self) -> None:
        """Load all SDE data from bz2 files."""
        if self.loaded:
            return

        # Load categories first (needed for group mapping)
        self._load_csv("invCategories.csv", lambda row: {
            "id": int(row["categoryID"]),
            "name": row["categoryName"]
        }, lambda item: self._add_category(item))

        # Load groups (for category mapping)
        self._load_csv("invGroups.csv", lambda row: {
            "id": int(row["groupID"]),
            "category_id": int(row["categoryID"])
        }, lambda item: self._add_group(item))

        # Load invTypes.csv for type information
        self._load_csv("invTypes.csv", lambda row: {
            "id": int(row["typeID"]),
            "name": row["typeName"],
            "group_id": int(row.get("groupID", 0)),
            "mass": float(row.get("mass", 0)) if row.get("mass") and row["mass"] != "0E-10" else 0.0,
            "volume": float(row.get("volume", 0)) if row.get("volume") and row["volume"] != "0E-10" else 0.0,
            "portion_size": int(row.get("portionSize", 1)) if row.get("portionSize") else 1,
            "base_price": float(row["basePrice"]) if row.get("basePrice") and row["basePrice"] not in ("None", "") else None,
            "published": int(row.get("published", 0))
        }, lambda item: self._add_type_extended(item))

        # Load invTypeMaterials for reprocessing materials
        self._load_csv("invTypeMaterials.csv", lambda row: {
            "type_id": int(row["typeID"]),
            "material_type_id": int(row["materialTypeID"]),
            "quantity": int(row["quantity"])
        }, lambda item: self._add_material(item))

        # Load dgmTypeAttributes for skill attributes
        self._load_csv("dgmTypeAttributes.csv", lambda row: {
            "type_id": int(row["typeID"]),
            "attribute_id": int(row["attributeID"]),
            "value": float(row["valueFloat"]) if row.get("valueFloat") and row["valueFloat"] != "None" else (float(row["valueInt"]) if row.get("valueInt") and row["valueInt"] != "None" else 0.0)
        }, lambda item: self._add_attribute(item))

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

    def _add_category(self, item) -> None:
        self.categories[item["id"]] = item["name"]

    def _add_group(self, item) -> None:
        self.groups[item["id"]] = item["category_id"]

    def _add_type_extended(self, item) -> None:
        type_info = TypeInfo(
            id=item["id"],
            name=item["name"],
            group_id=item["group_id"],
            category_id=self.groups.get(item["group_id"], 0),
            volume=item["volume"],
            mass=item["mass"],
            portion_size=item["portion_size"],
            base_price=item["base_price"],
            published=item["published"]
        )
        self.type_names[type_info.id] = type_info.name
        self.type_ids[type_info.name] = type_info.id
        self.types[type_info.id] = type_info

    def _add_material(self, item) -> None:
        if item["type_id"] in self.types:
            self.types[item["type_id"]].materials.append((item["material_type_id"], item["quantity"]))

    def _add_attribute(self, item) -> None:
        if item["type_id"] in self.types:
            self.types[item["type_id"]].attributes[item["attribute_id"]] = item["value"]

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

    def get_type_info(self, type_id: int) -> TypeInfo | None:
        """Get full type information by ID."""
        return self.types.get(type_id)

    def get_category_name(self, category_id: int) -> str:
        """Get category name by ID."""
        return self.categories.get(category_id, "")

    def is_ore_or_ice(self, type_id: int) -> bool:
        """Check if item is ore or ice (including compressed variants)."""
        if type_id not in self.types:
            return False
        cat = self.types[type_id].category_id
        cat_name = self.categories.get(cat, "").lower()
        return cat_name in ("asteroid", "ice")

    def is_gas(self, type_id: int) -> bool:
        """Check if item is gas (including compressed variants)."""
        if type_id not in self.types:
            return False
        cat_name = self.categories.get(self.types[type_id].category_id, "").lower()
        return cat_name == "gas" or "gas" in cat_name

    def get_portion_size(self, type_id: int) -> int:
        """Get portion size for an item."""
        if type_id in self.types:
            return max(self.types[type_id].portion_size, 1)
        return 1

    def get_volume(self, type_id: int) -> float:
        """Get volume for an item."""
        if type_id in self.types:
            return self.types[type_id].volume
        return 0.0

    def has_materials(self, type_id: int) -> bool:
        """Check if item can be reprocessed (has materials)."""
        if type_id not in self.types:
            return False
        return len(self.types[type_id].materials) > 0

    def get_materials(self, type_id: int) -> list[tuple[int, int]]:
        """Get materials for an item."""
        if type_id not in self.types:
            return []
        return self.types[type_id].materials


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


# API Key management
import secrets
import json
import base64
from typing import Optional


@dataclass
class ReprocessingConfig:
    """Configuration for reprocessing yields."""
    structure_type: str  # "npc_station", "citadel", "athanor", "tatara"
    rig: Optional[str]  # null, "t1", "t2"
    security: str  # "highsec", "lowsec", "nullsec"
    skills: dict[str, int]  # skill_name -> level
    implant: Optional[str]  # null, "RX-801", "RX-802", "RX-804"

    # Computed values
    Sm: float = 0.0  # Structure modifier
    Rm: int = 0  # Rig modifier
    Sec: float = 0.0  # Security modifier
    Im: float = 0.0  # Implant modifier

    # Skill modifiers
    R: int = 0  # Reprocessing skill level
    Re: int = 0  # Reprocessing Efficiency skill level
    Op: int = 0  # Ore/Moon/Gas Processing skill level

    # Fixed base for scrap
    base_yield: int = 50


class ConfigManager:
    """Manages API keys and configuration."""

    def __init__(self):
        self.configs: dict[str, ReprocessingConfig] = {}

    def create_config(self, config_data: dict) -> tuple[str, Optional[dict]]:
        """Create a new configuration and return an API key."""
        errors = []

        # Validate structure
        structure = config_data.get("structure", {})
        struct_type = structure.get("type", "npc_station")
        rig = structure.get("rig")
        security = structure.get("security", "highsec")

        valid_struct_types = ["npc_station", "citadel", "athanor", "tatara"]
        valid_rigs = [None, "t1", "t2"]
        valid_securities = ["highsec", "lowsec", "nullsec"]

        if struct_type not in valid_struct_types:
            errors.append({
                "code": "INVALID_VALUE",
                "path": "structure.type",
                "message": f"Invalid structure type: {struct_type}"
            })

        if rig is not None and rig not in valid_rigs:
            errors.append({
                "code": "INVALID_VALUE",
                "path": "structure.rig",
                "message": f"Invalid rig: {rig}"
            })

        if security not in valid_securities:
            errors.append({
                "code": "INVALID_VALUE",
                "path": "structure.security",
                "message": f"Invalid security status: {security}"
            })

        # npc_station must have rig=null
        if struct_type == "npc_station" and rig is not None:
            errors.append({
                "code": "INVALID_VALUE",
                "path": "structure.rig",
                "message": "npc_station cannot have a rig"
            })

        # Validate implant
        implant = config_data.get("implant")
        valid_implants = [None, "RX-801", "RX-802", "RX-804"]
        if implant is not None and implant not in valid_implants:
            errors.append({
                "code": "INVALID_VALUE",
                "path": "implant",
                "message": f"Invalid implant: {implant}"
            })

        # Validate skills
        skills = config_data.get("skills", {})
        for skill_name, level in skills.items():
            if not isinstance(level, int) or level < 0 or level > 5:
                errors.append({
                    "code": "INVALID_VALUE",
                    "path": f"skills.{skill_name}",
                    "message": f"Invalid skill level for {skill_name}: {level}"
                })

        if errors:
            return "", errors[0]

        # Calculate modifiers
        # Structure modifier Sm
        sm_map = {
            "npc_station": 0.00,
            "citadel": 0.00,
            "athanor": 0.02,
            "tatara": 0.055
        }
        Sm = sm_map.get(struct_type, 0.0)

        # Rig modifier Rm
        rm_map = {
            None: 0,
            "t1": 1,
            "t2": 3
        }
        Rm = rm_map.get(rig, 0)

        # Security modifier Sec
        sec_map = {
            "highsec": 0.00,
            "lowsec": 0.06,
            "nullsec": 0.12
        }
        Sec = sec_map.get(security, 0.0)

        # Implant modifier Im
        im_map = {
            None: 0.00,
            "RX-801": 0.01,
            "RX-802": 0.02,
            "RX-804": 0.04
        }
        Im = im_map.get(implant, 0.0)

        # Skills
        R = skills.get("Reprocessing", 0)
        Re = skills.get("Reprocessing Efficiency", 0)
        Op = skills.get("Ore Processing", 0) or skills.get("Gas Cloud Processing", 0) or skills.get("Moon Mining", 0)

        config = ReprocessingConfig(
            structure_type=struct_type,
            rig=rig,
            security=security,
            skills=skills,
            implant=implant,
            Sm=Sm,
            Rm=Rm,
            Sec=Sec,
            Im=Im,
            R=R,
            Re=Re,
            Op=Op
        )

        # Generate API key
        api_key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode('ascii')
        self.configs[api_key] = config

        return api_key, None

    def get_config(self, api_key: str) -> Optional[ReprocessingConfig]:
        """Get configuration by API key."""
        return self.configs.get(api_key)


config_manager = ConfigManager()


def calculate_yield(config: ReprocessingConfig, is_ore_ice_gas: bool) -> float:
    """Calculate yield percentage based on configuration.

    For ore/ice/gas: Yield = (50 + Rm) * (1 + Sec) * (1 + Sm) * (1 + Sm_skill) * (1 + R*0.03) * (1 + Re*0.02) * (1 + Op*0.02) * (1 + Im)
    For scrap: max of 55 with base 50 + scrap reprocessing skill
    """
    if is_ore_ice_gas:
        # Ore/ice/gas yield formula
        yield_pct = (50 + config.Rm) * (1 + config.Sec) * (1 + config.Sm) * (1 + config.Re * 0.02) * (1 + config.R * 0.03) * (1 + config.Op * 0.02) * (1 + config.Im)
    else:
        # Scrap yield - max 55, base 50 + scrap reprocessing skill
        # For scrap, we use Re (Reprocessing Efficiency) as the skill
        yield_pct = min(55, 50 + config.Re)

    return round(yield_pct, 2)


def get_efficiency_multiplier(yield_pct: float) -> float:
    """Convert yield percentage to multiplier."""
    return yield_pct / 100.0


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


@app.route("/v1/config", methods=["POST"])
def create_config():
    """Create a new reprocessing configuration API key."""
    if not request.is_json:
        return jsonify({
            "error": "INVALID_FORMAT",
            "details": "Request must be JSON"
        }), 400

    config_data = request.get_json()

    api_key, error = config_manager.create_config(config_data or {})

    if error:
        return jsonify(error), 404

    return jsonify({"key": api_key}), 201


@app.route("/v1/reprocess", methods=["POST"])
def reprocess_items():
    """Reprocess items and return results."""
    if not request.is_json:
        return jsonify({
            "error": "INVALID_FORMAT",
            "details": "Request must be JSON"
        }), 400

    data_payload = request.get_json()

    if data_payload is None:
        return jsonify({
            "error": "INVALID_FORMAT",
            "details": "Request body must be valid JSON"
        }), 400

    items = data_payload.get("items", {})
    efficiency = data_payload.get("efficiency", {})
    drill_down = data_payload.get("drill_down", False)
    market = data_payload.get("market")

    # Validate items
    if not isinstance(items, dict):
        return jsonify({
            "error": "INVALID_FORMAT",
            "details": "items must be a dictionary"
        }), 400

    # Validate quantities are positive integers
    for item_name, quantity in items.items():
        if not isinstance(quantity, int) or quantity <= 0:
            return jsonify({
                "error": "INVALID_FORMAT",
                "details": f"Quantity for {item_name} must be a positive integer"
            }), 400

    # Get API key if present
    api_key = request.headers.get("X-API-Key")
    config = config_manager.get_config(api_key) if api_key else None

    # Validate API key if provided
    if api_key and config is None:
        return jsonify({
            "code": "BAD_API_KEY",
            "message": "Invalid API key"
        }), 401

    # Default efficiency values
    default_efficiency = {
        "ore": 0.9063,
        "gas": 0.95,
        "scrap": 0.55
    }

    # Get efficiencies
    if config:
        # Use config-driven yields
        ore_yield = calculate_yield(config, True)
        gas_yield = calculate_yield(config, True)
        scrap_yield = calculate_yield(config, False)

        eff_ore = get_efficiency_multiplier(ore_yield)
        eff_gas = get_efficiency_multiplier(gas_yield)
        eff_scrap = get_efficiency_multiplier(scrap_yield)

        yields = {
            "ore": ore_yield,
            "gas": gas_yield,
            "scrap": scrap_yield
        }
    else:
        # Use provided efficiency values
        eff_ore = efficiency.get("ore", default_efficiency["ore"])
        eff_gas = efficiency.get("gas", default_efficiency["gas"])
        eff_scrap = efficiency.get("scrap", default_efficiency["scrap"])

        yields = {
            "ore": round(eff_ore * 100, 2),
            "gas": round(eff_gas * 100, 2),
            "scrap": round(eff_scrap * 100, 2)
        }

    # Validate efficiency values
    for eff_type, eff_val in [("ore", eff_ore), ("gas", eff_gas), ("scrap", eff_scrap)]:
        if not isinstance(eff_val, (int, float)) or eff_val < 0 or eff_val > 1:
            return jsonify({
                "error": "INVALID_FORMAT",
                "details": f"Efficiency for {eff_type} must be between 0 and 1"
            }), 400

    # Check for unknown items
    sde = data.sde
    if sde is None:
        return jsonify({
            "error": "SDE_NOT_LOADED",
            "details": "SDE data not loaded"
        }), 500

    unknown_items = []
    for item_name in items.keys():
        if item_name not in sde.type_ids:
            unknown_items.append(item_name)

    if unknown_items:
        return jsonify({
            "error": "UNKNOWN_ITEMS",
            "details": sorted(unknown_items)
        }), 404

    # Process items
    products = {}  # item_name -> {quantity, volume, buy, sell}
    input_volume = 0.0

    # First pass: collect all items to process
    items_to_process = {}  # item_id -> total_quantity
    for item_name, quantity in items.items():
        type_id = sde.type_ids[item_name]
        items_to_process[type_id] = items_to_process.get(type_id, 0) + quantity

        # Calculate input volume
        volume = sde.get_volume(type_id)
        if volume > 0:
            input_volume += quantity * volume

    # Process items with reprocessing
    outputs_to_process = {}  # item_id -> quantity for drill-down

    for type_id, total_quantity in items_to_process.items():
        type_info = sde.get_type_info(type_id)
        if type_info is None:
            continue

        portion_size = sde.get_portion_size(type_id)
        full_portions = total_quantity // portion_size
        leftover = total_quantity % portion_size

        # Check if item is reprocessable (has materials)
        if sde.has_materials(type_id):
            # Determine efficiency type
            if sde.is_ore_or_ice(type_id):
                efficiency_mult = eff_ore
            elif sde.is_gas(type_id):
                efficiency_mult = eff_gas
            else:
                efficiency_mult = eff_scrap

            # Sum materials from all full portions
            material_totals = {}  # material_type_id -> total_quantity_needed
            for _ in range(full_portions):
                for mat_type_id, mat_qty in sde.get_materials(type_id):
                    material_totals[mat_type_id] = material_totals.get(mat_type_id, 0) + mat_qty

            # Apply efficiency and round down
            for mat_type_id, total_qty in material_totals.items():
                result_qty = int(total_qty * efficiency_mult)
                if result_qty > 0:
                    mat_name = sde.get_type_name(mat_type_id)
                    if mat_name:
                        if mat_name not in outputs_to_process:
                            outputs_to_process[mat_name] = 0
                        outputs_to_process[mat_name] += result_qty
        else:
            # Non-reprocessable item - add to products as leftover
            item_name = sde.get_type_name(type_id)
            if item_name:
                if item_name not in products:
                    products[item_name] = {"quantity": 0, "volume": 0.0, "buy": None, "sell": None}
                products[item_name]["quantity"] += total_quantity

        # Add leftover units to products
        if leftover > 0:
            item_name = sde.get_type_name(type_id)
            if item_name:
                if item_name not in products:
                    products[item_name] = {"quantity": 0, "volume": 0.0, "buy": None, "sell": None}
                products[item_name]["quantity"] += leftover

    # Add initial outputs to products
    for item_name, quantity in outputs_to_process.items():
        if item_name not in products:
            products[item_name] = {"quantity": 0, "volume": 0.0, "buy": None, "sell": None}
        products[item_name]["quantity"] += quantity

    # Drill-down reprocessing
    if drill_down:
        products = drill_down_reprocess(products, config if config else None,
                                        eff_ore if not config else None,
                                        eff_gas if not config else None,
                                        eff_scrap if not config else None,
                                        yields if not config else None)

    # Calculate volumes and prices for products
    output_volume = 0.0

    # Determine which market to use for pricing
    market_name = None
    if market and market in MAIN_HUBS:
        market_name = market

    for item_name, product in products.items():
        type_id = sde.type_ids.get(item_name)
        if type_id:
            product["volume"] = round(product["quantity"] * sde.get_volume(type_id), 2)
            output_volume += product["quantity"] * sde.get_volume(type_id)

            # Get market prices if available
            if market_name:
                type_orders = data.get_orders_for_market(market_name, type_id)
                if type_orders:
                    sell_orders = [o for o in type_orders if not o.is_buy_order]
                    buy_orders = [o for o in type_orders if o.is_buy_order]

                    filtered_sell = filter_outlier_orders(sell_orders, False)
                    filtered_buy = filter_outlier_orders(buy_orders, True)

                    if filtered_buy:
                        product["buy"] = round(max(o.price for o in filtered_buy), 2)
                    if filtered_sell:
                        product["sell"] = round(min(o.price for o in filtered_sell), 2)

    output_volume = round(output_volume, 2)

    # Calculate input buy/sell values (weighted average of all input items)
    input_buy = None
    input_sell = None
    input_buy_5pct = None
    input_sell_5pct = None

    if market_name:
        all_orders = []
        for type_id in items_to_process.keys():
            all_orders.extend(data.get_orders_for_market(market_name, type_id))

        if all_orders:
            sell_orders = [o for o in all_orders if not o.is_buy_order]
            buy_orders = [o for o in all_orders if o.is_buy_order]

            filtered_sell = filter_outlier_orders(sell_orders, False)
            filtered_buy = filter_outlier_orders(buy_orders, True)

            if filtered_buy:
                input_buy = round(max(o.price for o in filtered_buy), 2)
            if filtered_sell:
                input_sell = round(min(o.price for o in filtered_sell), 2)

            input_buy_5pct = round(calculate_volume_weighted_price(filtered_buy, True), 2) if filtered_buy else None
            input_sell_5pct = round(calculate_volume_weighted_price(filtered_sell, False), 2) if filtered_sell else None

    # Calculate output buy/sell values
    output_buy = None
    output_sell = None
    output_buy_5pct = None
    output_sell_5pct = None

    if market_name and products:
        all_output_orders = []
        for item_name in products.keys():
            type_id = sde.type_ids.get(item_name)
            if type_id:
                all_output_orders.extend(data.get_orders_for_market(market_name, type_id))

        if all_output_orders:
            sell_orders = [o for o in all_output_orders if not o.is_buy_order]
            buy_orders = [o for o in all_output_orders if o.is_buy_order]

            filtered_sell = filter_outlier_orders(sell_orders, False)
            filtered_buy = filter_outlier_orders(buy_orders, True)

            if filtered_buy:
                output_buy = round(max(o.price for o in filtered_buy), 2)
            if filtered_sell:
                output_sell = round(min(o.price for o in filtered_sell), 2)

            output_buy_5pct = round(calculate_volume_weighted_price(filtered_buy, True), 2) if filtered_buy else None
            output_sell_5pct = round(calculate_volume_weighted_price(filtered_sell, False), 2) if filtered_sell else None

    # If market is null or no market data, set all prices to None
    if not market_name or market == "null":
        input_buy = input_sell = input_buy_5pct = input_sell_5pct = None
        output_buy = output_sell = output_buy_5pct = output_sell_5pct = None
        for product in products.values():
            product["buy"] = None
            product["sell"] = None

    result = {
        "inputs": {
            "volume": round(input_volume, 2),
            "buy": input_buy,
            "sell": input_sell,
            "buy_5pct": input_buy_5pct,
            "sell_5pct": input_sell_5pct,
        },
        "outputs": {
            "volume": output_volume,
            "buy": output_buy,
            "sell": output_sell,
            "buy_5pct": output_buy_5pct,
            "sell_5pct": output_sell_5pct,
        },
        "yields": yields,
        "products": products
    }

    return jsonify(result), 201


def drill_down_reprocess(products: dict, config: Optional[ReprocessingConfig],
                          eff_ore: Optional[float], eff_gas: Optional[float],
                          eff_scrap: Optional[float], yields: Optional[dict]) -> dict:
    """Reprocess outputs recursively until nothing further can be broken down."""

    sde = data.sde
    if sde is None:
        return products

    changed = True
    while changed:
        changed = False
        new_products = {}

        for item_name, product in products.items():
            quantity = product["quantity"]
            type_id = sde.type_ids.get(item_name)

            if type_id is None or not sde.has_materials(type_id):
                # Can't reprocess this item
                if item_name not in new_products:
                    new_products[item_name] = {"quantity": 0, "volume": 0.0, "buy": None, "sell": None}
                new_products[item_name]["quantity"] += quantity
                continue

            # Determine efficiency to use
            if config:
                if sde.is_ore_or_ice(type_id) or sde.is_gas(type_id):
                    efficiency_mult = get_efficiency_multiplier(calculate_yield(config, True))
                else:
                    efficiency_mult = get_efficiency_multiplier(calculate_yield(config, False))
            else:
                if sde.is_ore_or_ice(type_id):
                    efficiency_mult = eff_ore
                elif sde.is_gas(type_id):
                    efficiency_mult = eff_gas
                else:
                    efficiency_mult = eff_scrap

            portion_size = sde.get_portion_size(type_id)
            full_portions = quantity // portion_size
            leftover = quantity % portion_size

            if full_portions > 0:
                # Sum materials from all full portions
                material_totals = {}
                for _ in range(full_portions):
                    for mat_type_id, mat_qty in sde.get_materials(type_id):
                        material_totals[mat_type_id] = material_totals.get(mat_type_id, 0) + mat_qty

                # Apply efficiency and round down
                for mat_type_id, total_qty in material_totals.items():
                    result_qty = int(total_qty * efficiency_mult)
                    if result_qty > 0:
                        mat_name = sde.get_type_name(mat_type_id)
                        if mat_name:
                            if mat_name not in new_products:
                                new_products[mat_name] = {"quantity": 0, "volume": 0.0, "buy": None, "sell": None}
                            new_products[mat_name]["quantity"] += result_qty
                            changed = True
            else:
                changed = True

            # Add leftover units
            if leftover > 0:
                if item_name not in new_products:
                    new_products[item_name] = {"quantity": 0, "volume": 0.0, "buy": None, "sell": None}
                new_products[item_name]["quantity"] += leftover

        products = new_products

    return products


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
