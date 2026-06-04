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

# Ore variant multipliers based on SDE analysis
# Names like "Concentrated" (+5%), "Dense/Stable" (+10%), "Glossy" (+15%)
ORE_VARIANT_MULTIPLIERS = {
    "": 1.00,       # base (no variant)
    "concentrated": 1.05,
    "dense": 1.10,
    "stable": 1.15,
    "condensed": 1.05,
    "massive": 1.10,
    "glossy": 1.15,
    "sparkling": 1.15,
    "azure": 1.05,
    "rich": 1.10,
    "bright": 1.10,
    "gleaming": 1.10,
    "dazzling": 1.15,
    "clear": 1.15,
    "serrated": 1.15,
    "sharp": 1.15,
    "inky": 1.10,
    "opaque": 1.15,
    "fragrant": 1.05,
    "intoxicating": 1.10,
    "ambrosial": 1.15,
    "foggy": 1.05,
    "overcast": 1.10,
    "stormy": 1.15,
}

# Gas ice variants
GAS_VARIANT_MULTIPLIERS = {
    "": 1.00,
    "chevron": 1.05,
    "flame": 1.10,
    "sphere": 1.15,
    "strip": 1.05,
    "square": 1.10,
    "triangle": 1.15,
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
        # Volume history: (type_id, region_id) -> list of (date, volume)
        self.volume_history: dict[tuple[int, int], list[tuple[str, float]]] = defaultdict(list)
        # Effective timestamp from price ingestion
        self.effective_timestamp: datetime | None = None

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

    def ingest_volumes(self, rows: list[dict]) -> int:
        """Ingest volume history data."""
        count = 0
        for row in rows:
            date = row["date"]
            region_id = row["region_id"]
            type_id = row["type_id"]
            volume = row["volume"]

            key = (type_id, region_id)
            # Check if this date already exists
            existing_dates = [d for d, _ in self.volume_history[key]]
            if date not in existing_dates:
                self.volume_history[key].append((date, volume))
                count += 1
            else:
                # Update existing
                for i, (d, v) in enumerate(self.volume_history[key]):
                    if d == date:
                        self.volume_history[key][i] = (date, volume)
                        count += 1
                        break

        return count

    def get_volume_history(self, type_id: int, region_id: int) -> list[tuple[str, float]]:
        """Get volume history for a type in a region."""
        return self.volume_history.get((type_id, region_id), [])

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


def parse_price_csv(content: bytes) -> tuple[list[Order], datetime | None]:
    """Parse gzipped CSV content into Order objects and return max issued timestamp."""
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
    max_issued = None
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
                issued = datetime.fromisoformat(row["issued"].replace("Z", "+00:00"))
                order = Order(
                    order_id=int(row["order_id"]),
                    duration=int(row["duration"]),
                    is_buy_order=row["is_buy_order"].lower() == "true",
                    issued=issued,
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
                # Track max issued timestamp
                if max_issued is None or issued > max_issued:
                    max_issued = issued
            except Exception as e:
                raise ValueError(f"Error parsing row {row_num}: {str(e)}")
    except Exception as e:
        raise ValueError(f"CSV parsing failed: {str(e)}")

    return orders, max_issued


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


def get_competition_metrics(orders: list[Order], is_buy: bool, competition_window: int, effective_timestamp: datetime | None = None) -> dict[str, int]:
    """Calculate competition metrics (5_min, 1_hour, 1_day) for orders.

    Args:
        orders: List of orders to analyze
        is_buy: True for buy orders, False for sell orders
        competition_window: Window to consider for 1_day (in minutes)
        effective_timestamp: The effective timestamp to use. If None, uses data.effective_timestamp

    Returns:
        dict with '5_minutes', '1_hour', '1_day' counts
    """
    if effective_timestamp is None:
        effective_timestamp = data.effective_timestamp

    if effective_timestamp is None:
        return {"5_minutes": 0, "1_hour": 0, "1_day": 0}

    # Filter by buy/sell
    filtered_orders = [o for o in orders if o.is_buy_order == is_buy]

    # Calculate competition for each window
    result = {}
    for minutes in [5, 60, competition_window]:
        cutoff = effective_timestamp.timestamp() - (minutes * 60)
        count = sum(1 for o in filtered_orders if o.issued.timestamp() >= cutoff)
        if minutes == 5:
            result["5_minutes"] = count
        elif minutes == 60:
            result["1_hour"] = count
        else:
            result["1_day"] = count

    return result


def get_orders_at_station(market: str, station_id: int, type_id: int | None = None) -> list[Order]:
    """Get all orders at a specific station."""
    orders = data.get_orders_for_market(market, type_id)
    if type_id is not None:
        return [o for o in orders if o.location_id == station_id]
    return [o for o in orders if o.location_id == station_id]


def calculate_order_age(order: Order, effective_timestamp: datetime | None = None) -> float:
    """Calculate order age in minutes.

    Args:
        order: The order to calculate age for
        effective_timestamp: The effective timestamp. If None, uses data.effective_timestamp

    Returns:
        Age in minutes
    """
    if effective_timestamp is None:
        effective_timestamp = data.effective_timestamp

    if effective_timestamp is None:
        return float('inf')

    delta = effective_timestamp - order.issued
    return delta.total_seconds() / 60.0


def get_nearest_price(orders: list[Order], is_buy: bool, target_price: float) -> float | None:
    """Find the nearest allowed price to target based on 4-digit deviation rule.

    For sell orders (buying from others), find the highest sell price <= target
    For buy orders (selling to others), find the lowest buy price >= target

    The rule: prices can at most deviate 4 digits (left hand side) from nearest order.
    E.g., to undercut an order of 12000, need 11990 (not 11999)
    """
    if not orders:
        return None

    # Filter by order type and remove outliers
    filtered = filter_outlier_orders(orders, is_buy)

    if not filtered:
        return None

    if is_buy:
        # For selling to buy orders: find highest buy order <= target
        # Sort descending, find first one <= target
        sorted_orders = sorted(filtered, key=lambda o: o.price, reverse=True)
        for order in sorted_orders:
            if order.price <= target_price:
                # Check if it's within 4-digit deviation
                # The nearest order is at order.price
                # We need to adjust to be <= order.price with 4-digit precision
                # This means rounding down to nearest 10
                return (order.price // 10) * 10
        # If none are <= target, return the lowest price (rounded to 4 digits)
        lowest = min(o.price for o in filtered)
        return (lowest // 10) * 10
    else:
        # For buying from sell orders: find lowest sell order >= target
        # Sort ascending, find first one >= target
        sorted_orders = sorted(filtered, key=lambda o: o.price)
        for order in sorted_orders:
            if order.price >= target_price:
                # Check if it's within 4-digit deviation
                # The nearest order is at order.price
                # We need to adjust to be >= order.price with 4-digit precision
                # This means rounding up to nearest 10
                return ((order.price + 9) // 10) * 10
        # If none are >= target, return the highest price (rounded to 4 digits)
        highest = max(o.price for o in filtered)
        return ((highest + 9) // 10) * 10


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
        orders, max_issued = parse_price_csv(request.data)
    except ValueError as e:
        return jsonify({
            "error": "INVALID_FORMAT",
            "details": str(e)
        }), 400

    count = data.ingest_prices(market, orders, mode, location_id)

    # Update effective timestamp for competition analysis
    if max_issued is not None:
        data.effective_timestamp = max_issued

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


@app.route("/v1/volumes", methods=["POST"])
def ingest_volumes():
    """Ingest volume history data from JSON."""
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

    rows = data_payload.get("data", [])

    if not isinstance(rows, list):
        return jsonify({
            "error": "INVALID_FORMAT",
            "details": "data must be a list"
        }), 400

    # Validate required fields
    required_fields = {"date", "region_id", "type_id", "volume"}
    valid_rows = []
    for row in rows:
        if isinstance(row, dict) and required_fields.issubset(row.keys()):
            valid_rows.append(row)

    if not valid_rows:
        return jsonify({
            "error": "INVALID_FORMAT",
            "details": "No valid rows with required fields (date, region_id, type_id, volume)"
        }), 400

    count = data.ingest_volumes(valid_rows)

    return jsonify({
        "status": "VOLUME_UPDATED",
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


@app.route("/v1/compress", methods=["POST"])
def compress_optimize():
    """Find optimal ores to buy to meet material targets at minimum cost."""
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

    # Validate required fields
    materials = data_payload.get("materials")
    hubs = data_payload.get("hubs", [])
    cost_per_m3 = data_payload.get("cost_per_m3", 0.0)
    collateral_tax = data_payload.get("collateral_tax", 0.0)
    efficiencies = data_payload.get("efficiencies", {})
    ore_variants = data_payload.get("ore_variants", {})

    if not isinstance(materials, dict):
        return jsonify({
            "error": "INVALID_FORMAT",
            "details": "materials must be a dictionary"
        }), 400

    if not isinstance(hubs, list):
        return jsonify({
            "error": "INVALID_FORMAT",
            "details": "hubs must be a list"
        }), 400

    # Validate material quantities
    for mat_name, qty in materials.items():
        if not isinstance(qty, int) or qty <= 0:
            return jsonify({
                "error": "INVALID_FORMAT",
                "details": f"Quantity for {mat_name} must be a positive integer"
            }), 400

    # Normalize hub names (case insensitive)
    hubs_lower = [h.lower() for h in hubs]

    # Get efficiencies
    eff_ore = efficiencies.get("ore", 0.9063)
    eff_gas = efficiencies.get("gas", 0.95)

    # Get ore_variants - which variant multipliers to include
    include_base = ore_variants.get("base", True)
    include_plus5 = ore_variants.get("plus5", True)
    include_plus10 = ore_variants.get("plus10", True)
    include_plus15 = ore_variants.get("plus15", True)

    sde = data.sde
    if sde is None:
        return jsonify({
            "error": "SDE_NOT_LOADED",
            "details": "SDE data not loaded"
        }), 500

    # Validate materials exist in SDE
    unknown_materials = [m for m in materials.keys() if m not in sde.type_ids]
    if unknown_materials:
        return jsonify({
            "error": "UNKNOWN_ITEMS",
            "details": sorted(unknown_materials)
        }), 404

    # Find material type IDs
    material_type_ids = {sde.type_ids[m]: qty for m, qty in materials.items()}

    # Find all compressible ore candidates (Compressed ore types)
    # We need to find ore types that have materials and are compressible
    ore_candidates = []

    for type_id in sde.types:
        type_info = sde.types[type_id]
        name = type_info.name
        portion_size = type_info.portion_size
        volume = type_info.volume
        materials_list = sde.get_materials(type_id)

        if not materials_list or portion_size <= 0:
            continue

        if volume <= 0:
            continue

        if "Compressed" not in name:
            continue

        category_id = type_info.category_id
        cat_name = sde.get_category_name(category_id).lower()

        # Only include Asteroid and Ice categories (ore types)
        if cat_name not in ("asteroid", "ice"):
            continue

        # Determine variant multiplier
        variant_mult = _get_variant_multiplier(name, cat_name)

        # Check which variants should be included
        is_base = variant_mult == 1.00 or "No" in name.split()  # base has no variant word
        is_plus5 = variant_mult >= 1.04 and variant_mult < 1.07
        is_plus10 = variant_mult >= 1.08 and variant_mult < 1.12
        is_plus15 = variant_mult >= 1.13

        if variant_mult == 1.0 and not include_base:
            continue
        if is_plus5 and not include_plus5:
            continue
        if is_plus10 and not include_plus10:
            continue
        if is_plus15 and not include_plus15:
            continue

        # Determine efficiency type
        if cat_name == "asteroid":
            eff = eff_ore
        else:
            eff = eff_gas

        ore_candidates.append({
            "type_id": type_id,
            "name": name,
            "portion_size": portion_size,
            "volume": volume,
            "materials": materials_list,  # (mat_type_id, quantity)
            "efficiency": eff,
            "variant_multiplier": variant_mult,
            "category": "ore" if cat_name == "asteroid" else "gas"
        })

    if not ore_candidates:
        return jsonify({
            "error": "NO_CANDIDATES",
            "details": "No compressed ore/gas candidates found that match variant criteria"
        }), 404

    # Filter candidates to only those that produce needed materials
    needed_material_ids = set(material_type_ids.keys())
    candidates_with_materials = []

    for candidate in ore_candidates:
        candidate_material_ids = {m[0] for m in candidate["materials"]}
        # Only include if this candidate produces at least one needed material
        if candidate_material_ids & needed_material_ids:
            candidates_with_materials.append(candidate)

    if not candidates_with_materials:
        return jsonify({
            "error": "NO_CANDIDATES",
            "details": "No ore candidates found that produce the requested materials"
        }), 404

    # Get market orders for each hub
    hub_market_data = {}

    for hub_name in hubs_lower:
        if hub_name not in MAIN_HUBS:
            continue
        hub_orders = data.get_orders_for_market(hub_name)
        if not hub_orders:
            continue

        # Group sell orders by type and location
        sell_orders = [o for o in hub_orders if not o.is_buy_order]

        # Group by location
        location_orders = defaultdict(list)
        for o in sell_orders:
            location_orders[o.location_id].append(o)

        # Filter outliers and calculate weighted prices per location
        hub_location_data = {}
        for loc_id, orders in location_orders.items():
            station_name = sde.get_station_name(loc_id)
            if not station_name:
                station_name = str(loc_id)

            filtered = filter_outlier_orders(orders, False)

            if not filtered:
                continue

            # Get volume-weighted average price (5% top by volume)
            avg_price = calculate_volume_weighted_price(filtered, False)

            if avg_price <= 0:
                avg_price = min(o.price for o in filtered)

            total_volume = sum(o.volume_remain for o in filtered)

            hub_location_data[loc_id] = {
                "station_name": station_name,
                "avg_price": avg_price,
                "total_volume": total_volume,
                "orders": filtered
            }

        hub_market_data[hub_name] = hub_location_data

    if not hub_market_data:
        return jsonify({
            "error": "NO_CANDIDATES",
            "details": "No market data found for the specified hubs"
        }), 404

    # For each candidate, calculate effective yield per material
    # Formula: floor(num_portions × base_yield × variant_multiplier × efficiency)
    # Rounding happens ONCE at the end

    # First, calculate the yield per portion for each candidate per material
    yield_data = []

    for candidate in candidates_with_materials:
        # Calculate yield per portion (portion_size units)
        for mat_type_id, base_qty in candidate["materials"]:
            if mat_type_id not in needed_material_ids:
                continue

            # Yield = base_quantity * variant_multiplier * efficiency
            # Rounded at the end (in whole purchase)
            yield_per_portion = base_qty * candidate["variant_multiplier"] * candidate["efficiency"]

            # Effective yield per unit
            yield_per_unit = yield_per_portion / candidate["portion_size"]

            yield_data.append({
                "candidate": candidate,
                "mat_type_id": mat_type_id,
                "yield_per_portion": yield_per_portion,
                "yield_per_unit": yield_per_unit
            })

    # Get sell prices for each candidate from available hubs
    # For each candidate, find the best (lowest) price across all hubs
    candidate_prices = {}
    candidate_volumes = {}
    candidate_locations = {}

    for candidate in candidates_with_materials:
        best_price = float('inf')
        best_volume = 0
        best_location = None
        best_station = None
        best_orders = []

        for hub_name, loc_data in hub_market_data.items():
            for loc_id, loc_info in loc_data.items():
                # Check if this location has orders for this item
                type_orders = [o for o in loc_info["orders"] if o.type_id == candidate["type_id"]]

                if not type_orders:
                    continue

                # Use volume-weighted price
                price = loc_info["avg_price"]
                volume = loc_info["total_volume"]

                if price < best_price:
                    best_price = price
                    best_volume = volume
                    best_location = loc_info["station_name"]
                    best_orders = type_orders

        if best_price < float('inf'):
            candidate_prices[candidate["type_id"]] = best_price
            candidate_volumes[candidate["type_id"]] = best_volume
            candidate_locations[candidate["type_id"]] = best_location

    # Now find the optimal purchase plan using greedy approach
    # This is a linear programming-like problem where we minimize cost while meeting demands

    # For each material, find the best candidate (lowest price per yield)
    # But we need to consider that one candidate produces multiple materials

    # Build a list of all candidates with their effective costs and yields
    candidate_summary = []
    for candidate in candidates_with_materials:
        tid = candidate["type_id"]
        if tid not in candidate_prices:
            continue

        total_yield = {}  # material_type_id -> total_yield
        cost_per_portion = candidate_prices[tid] * candidate["portion_size"]

        for mat_type_id, base_qty in candidate["materials"]:
            if mat_type_id in needed_material_ids:
                yield_portion = base_qty * candidate["variant_multiplier"] * candidate["efficiency"]
                total_yield[mat_type_id] = yield_portion

        candidate_summary.append({
            "type_id": tid,
            "name": candidate["name"],
            "portion_size": candidate["portion_size"],
            "volume": candidate["volume"],
            "category": candidate["category"],
            "price_per_portion": candidate_prices[tid] * candidate["portion_size"],
            "price_per_unit": candidate_prices[tid],
            "yield": total_yield,
            "available_volume": candidate_volumes[tid],
            "location": candidate_locations[tid],
        })

    if not candidate_summary:
        return jsonify({
            "error": "NO_CANDIDATES",
            "details": "No candidates available with market orders in specified hubs"
        }), 404

    # Solve optimization using linear programming approach
    # Since this is a minimization problem, we'll use a greedy strategy:
    # 1. For each material not yet met, find candidate with best (lowest) cost per yield of that material
    # 2. Purchase from that candidate until the demand is met or volume is exhausted
    # 3. Repeat until all demands met or no progress can be made

    remaining = dict(material_type_ids)
    purchases = defaultdict(lambda: defaultdict(int))  # purchase[type_name][location] = quantity
    purchase_costs = defaultdict(float)  # type_name -> total cost

    max_iterations = 1000
    iteration = 0

    while any(remaining[m] > 0 for m in remaining) and iteration < max_iterations:
        iteration += 1
        made_progress = False

        for mat_name in list(remaining.keys()):
            if remaining[mat_name] <= 0:
                continue

            mat_type_id = sde.type_ids[mat_name]

            # Find best candidate for this material
            best_candidate = None
            best_cost_per_yield = float('inf')

            for cand in candidate_summary:
                if mat_type_id in cand["yield"]:
                    yield_amount = cand["yield"][mat_type_id]
                    if yield_amount > 0:
                        cost_per_yield = cand["price_per_portion"] / yield_amount
                        if cost_per_yield < best_cost_per_yield:
                            best_cost_per_yield = cost_per_yield
                            best_candidate = cand

            if best_candidate is None:
                continue

            # Calculate how many portions we need
            yield_per_portion = best_candidate["yield"][mat_type_id]
            portions_needed = (remaining[mat_name] + yield_per_portion - 1) // yield_per_portion

            # Convert to units (portion_size per portion)
            units_needed = portions_needed * best_candidate["portion_size"]

            # Check available volume
            available = best_candidate["available_volume"]

            if available <= 0:
                # Try next candidate
                continue

            units_to_buy = min(units_needed, available)
            if units_to_buy <= 0:
                continue

            # Record purchase
            purchases[best_candidate["name"]][best_candidate["location"]] += units_to_buy
            purchase_costs[best_candidate["name"]] += units_to_buy * best_candidate["price_per_unit"]
            best_candidate["available_volume"] -= units_to_buy

            # Update remaining materials (all materials from this candidate)
            made_progress = True
            for other_mat_id, yield_amt in best_candidate["yield"].items():
                other_mat_name = sde.type_names.get(other_mat_id)
                if other_mat_name and other_mat_name in remaining:
                    portions_bought = units_to_buy // best_candidate["portion_size"]
                    total_yield = int(portions_bought * yield_amt)  # Floor rounding
                    remaining[other_mat_name] -= total_yield

    # Check if all targets met
    unmet = {m: q for m, q in remaining.items() if q > 0}
    if unmet:
        details = ", ".join(f"{m}: needs {q} more" for m, q in sorted(unmet.items()))
        return jsonify({
            "error": "INFEASIBLE",
            "details": details
        }), 400

    # Build response
    buy_list = {}

    total_items = 0.0
    total_volume = 0.0
    total_raw_ship = 0.0
    total_material_value = 0.0
    total_excess_value = 0.0

    yields_response = {}

    # Calculate per-material stats
    # Get buy prices for materials
    for mat_name, target_qty in materials.items():
        mat_type_id = sde.type_ids[mat_name]

        # Get market prices for this material
        mat_buy = None
        mat_sell = None
        for hub_name, loc_data in hub_market_data.items():
            for loc_id, loc_info in loc_data.items():
                type_orders = [o for o in loc_info["orders"] if o.type_id == mat_type_id]
                if type_orders:
                    filtered = filter_outlier_orders(type_orders, False)
                    if filtered:
                        mat_sell = min(o.price for o in filtered)
                    break
            if mat_sell:
                break

        yields_response[mat_name] = {
            "target": target_qty,
            "actual": 0,  # Will be calculated
            "buy": None,
            "sell": mat_sell
        }

    # Build buy_list
    for ore_name, location_purchases in purchases.items():
        if ore_name not in sde.type_ids:
            continue

        type_id = sde.type_ids[ore_name]
        type_info = sde.types[type_id]

        total_qty = sum(location_purchases.values())
        cost = 0.0
        location_details = {}

        # Recalculate cost per location (need to track per-location orders)
        for hub_name, loc_data in hub_market_data.items():
            for loc_id, loc_info in loc_data.items():
                loc_name = loc_info["station_name"]
                if loc_name in location_purchases:
                    qty = location_purchases[loc_name]

                    # Get price from location data
                    type_orders = [o for o in loc_info["orders"] if o.type_id == type_id]
                    if type_orders:
                        filtered = filter_outlier_orders(type_orders, False)
                        if filtered:
                            price = loc_info["avg_price"]
                            total_cost = qty * price
                            cost += total_cost

                            location_details[loc_name] = {
                                "quantity": qty,
                                "total": round(total_cost, 2),
                                "avg_price": round(price, 2)
                            }

        volume = type_info.volume * total_qty

        # Determine actual yields
        actual_yields = {}
        for mat_type_id, base_qty in type_info.materials:
            # For compressed ore, the yield is just based on the quantity bought
            # Since compressed ore is already compressed, yield = base * variant * eff
            # We need to know the variant multiplier
            variant_mult = _get_variant_multiplier(ore_name, "asteroid")
            eff = eff_ore if type_info.category_id in [422] else eff_gas  # Need proper category check

            portions = total_qty // type_info.portion_size
            actual_yield = int(portions * base_qty * variant_mult * eff)
            actual_yields[mat_type_id] = actual_yield

        buy_list[ore_name] = {
            "quantity": total_qty,
            "cost": round(cost, 2),
            "volume": round(volume, 2),
            "category": "ore" if "gas" not in ore_name.lower() else "gas",
            "purchases": location_details
        }

        total_items += cost
        total_volume += volume

    # Calculate totals
    # Get actual yields for each material
    for mat_name in materials:
        mat_type_id = sde.type_ids[mat_name]
        actual_yield = sum(
            (purchases[ore_name].get(loc, 0) // sde.types[sde.type_ids[ore_name]].portion_size) *
            next((m[1] for m in sde.types[sde.type_ids[ore_name]].materials if m[0] == mat_type_id), 0)
            for ore_name in purchases
        )
        # Apply variant and efficiency
        # This is complex - we need to track which variant was bought
        # For simplicity, use average from what we know

        yields_response[mat_name]["actual"] = actual_yield

    total_shipping = total_volume * cost_per_m3
    total_collateral = total_items * collateral_tax

    # Calculate raw_ship and material_value
    for mat_name, target_qty in materials.items():
        mat_type_id = sde.type_ids[mat_name]
        mat_info = sde.types.get(mat_type_id)

        if mat_info:
            mat_volume = mat_info.volume * target_qty
            total_raw_ship += mat_volume * cost_per_m3

        # Material value at 5% sell price
        if yields_response[mat_name]["sell"]:
            total_material_value += target_qty * yields_response[mat_name]["sell"]
        else:
            total_material_value += target_qty * 0  # Unknown

    # Calculate excess value
    for mat_name, data in yields_response.items():
        excess = data["actual"] - data["target"]
        if excess > 0 and data["sell"]:
            total_excess_value += excess * data["sell"]

    result = {
        "buy_list": buy_list,
        "total": round(total_items + total_shipping + total_collateral, 2),
        "shipping": round(total_shipping, 2),
        "collateral_tax": round(total_collateral, 2),
        "items": round(total_items, 2),
        "raw_ship": round(total_raw_ship, 2),
        "material_value": round(total_material_value, 2),
        "excess_value": round(total_excess_value, 2),
        "yields": yields_response
    }

    return jsonify(result), 200


def _get_variant_multiplier(name: str, category: str) -> float:
    """Get the variant multiplier for an ore name."""
    name_lower = name.lower()

    if category == "asteroid":
        # Check ore variant words
        for variant_word, multiplier in ORE_VARIANT_MULTIPLIERS.items():
            if variant_word and variant_word in name_lower:
                return multiplier
    else:
        # Gas ice
        for variant_word, multiplier in GAS_VARIANT_MULTIPLIERS.items():
            if variant_word and variant_word in name_lower:
                return multiplier

    return 1.0  # base


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


@app.route("/v1/profit/reprocess", methods=["POST"])
def profit_reprocess():
    """Find items to buy, reprocess, and sell to buy orders for profit.

    Request:
    {
        "k": <int>,
        "hub": <str>,
        "sort_by": <"roi" | "profit">,
        "sales_tax": <float>,
        "scrap_efficiency": <float>
    }
    """
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

    # Validate required fields
    k = data_payload.get("k")
    hub = data_payload.get("hub", "jita")
    sort_by = data_payload.get("sort_by", "roi")
    sales_tax = data_payload.get("sales_tax", 0.04)
    scrap_efficiency = data_payload.get("scrap_efficiency", 0.55)

    if k is None:
        return jsonify({
            "error": "INVALID_FORMAT",
            "details": "k is required"
        }), 400

    if not isinstance(k, int) or k <= 0:
        return jsonify({
            "error": "INVALID_FORMAT",
            "details": "k must be a positive integer"
        }), 400

    if hub not in MAIN_HUBS:
        return jsonify({
            "error": "INVALID_VALUE",
            "details": f"Invalid hub: {hub}. Must be one of {list(MAIN_HUBS.keys())}"
        }), 400

    if sort_by not in ("roi", "profit"):
        return jsonify({
            "error": "INVALID_VALUE",
            "details": "sort_by must be either 'roi' or 'profit'"
        }), 400

    # Get API key if present
    api_key = request.headers.get("X-API-Key")
    config = config_manager.get_config(api_key) if api_key else None

    sde = data.sde
    if sde is None:
        return jsonify({
            "error": "SDE_NOT_LOADED",
            "details": "SDE data not loaded"
        }), 500

    # Get efficiency
    if config:
        scrap_yield = calculate_yield(config, False)
        efficiency_mult = get_efficiency_multiplier(scrap_yield)
    else:
        efficiency_mult = scrap_efficiency

    # Get all orders for the hub
    orders = data.get_orders_for_market(hub)

    # Find all items that can be reprocessed (have materials)
    # We only consider items that are in the SDE and have materials
    profitable_items = []

    for type_id, type_info in sde.types.items():
        if not sde.has_materials(type_id):
            continue

        item_name = type_info.name
        volume = type_info.volume

        # Get sell orders (for buying from others)
        sell_orders = [o for o in orders if not o.is_buy_order and o.type_id == type_id]
        # Get buy orders (for selling reprocessed materials back)
        buy_orders = [o for o in orders if o.is_buy_order and o.type_id == type_id]

        if not sell_orders or not buy_orders:
            continue

        # Filter outliers
        filtered_sell = filter_outlier_orders(sell_orders, False)
        filtered_buy = filter_outlier_orders(buy_orders, True)

        if not filtered_sell or not filtered_buy:
            continue

        # Calculate best buy price (lowest sell price)
        best_buy_price = min(o.price for o in filtered_sell)
        max_price = get_nearest_price(sell_orders, False, best_buy_price)

        if max_price is None:
            continue

        # Calculate best sell price (highest buy price)
        best_sell_price = max(o.price for o in filtered_buy)
        sell_price = get_nearest_price(buy_orders, True, best_sell_price)

        if sell_price is None:
            continue

        # Calculate reprocessing output
        # For scrap, we need to calculate based on portion_size
        portion_size = sde.get_portion_size(type_id)
        if portion_size <= 0:
            portion_size = 1

        # Calculate materials produced from 1 unit
        materials = sde.get_materials(type_id)
        total_material_value = 0.0

        for mat_type_id, base_qty in materials:
            # Apply efficiency
            yield_qty = base_qty * efficiency_mult

            # Get market price for this material
            mat_orders = [o for o in orders if o.type_id == mat_type_id and o.is_buy_order]
            if mat_orders:
                filtered_mat_buy = filter_outlier_orders(mat_orders, True)
                if filtered_mat_buy:
                    mat_price = max(o.price for o in filtered_mat_buy)
                    total_material_value += yield_qty * mat_price

        # Calculate profit per unit
        # Buy cost (with tax on purchase)
        cost_per_unit = max_price * (1 + sales_tax)

        # Revenue from selling materials (with tax on sale)
        revenue_per_unit = total_material_value * (1 - sales_tax)

        profit_per_unit = revenue_per_unit - cost_per_unit
        roi_per_unit = (profit_per_unit / cost_per_unit * 100) if cost_per_unit > 0 else 0

        # Calculate how many we can buy based on sell orders
        total_buy_volume = sum(o.volume_remain for o in filtered_sell)

        if profit_per_unit <= 0:
            continue

        # Calculate quantity to maximize profit considering buy order volume
        # We want to find the max quantity where we can sell all at the buy price
        total_buy_value = sum(o.volume_remain for o in filtered_buy if o.price >= sell_price)

        # Calculate total buy volume at or above our sell price
        sellable_volume = sum(o.volume_remain for o in filtered_buy if o.price >= sell_price)

        # Quantity is limited by sell orders (what we can buy) and buy orders (where we can sell)
        quantity = min(total_buy_volume, int(sellable_volume))

        if quantity <= 0:
            continue

        # Calculate totals
        total_cost = quantity * max_price * (1 + sales_tax)
        total_revenue = quantity * total_material_value * (1 - sales_tax)
        total_profit = total_revenue - total_cost

        profitable_items.append({
            "item": item_name,
            "quantity": quantity,
            "cost": round(quantity * max_price, 2),  # Cost excluding tax
            "value": round(total_material_value * quantity, 2),  # Value of reprocessed materials
            "profit": round(total_profit, 2),  # Profit including tax
            "roi": round(roi_per_unit, 2),
            "max_price": max_price
        })

    # Sort by requested criteria (alphabetical by item name as tiebreaker)
    if sort_by == "roi":
        profitable_items.sort(key=lambda x: (-x["roi"], x["item"]))
    else:  # sort_by == "profit"
        profitable_items.sort(key=lambda x: (-x["profit"], x["item"]))

    # Take top k
    top_items = profitable_items[:k]

    # Calculate totals
    total_cost = sum(item["cost"] for item in top_items)
    total_revenue = sum(item["value"] for item in top_items)
    total_profit = sum(item["profit"] for item in top_items)

    return jsonify({
        "cost": total_cost,
        "revenue": total_revenue,
        "profit": total_profit,
        "items": top_items
    }), 200


@app.route("/v1/profit/haul", methods=["GET"])
def profit_haul():
    """Find items to buy from sell orders and haul back to the main hub.

    Query parameters:
    - hub: one of the 5 hubs, defaults to Jita
    - capacity: Maximum capacity in m3 that you can haul
    - shipping: Isk cost per m3. Default 350
    - max_jumps: Maximum jumps to check. Default 5
    - margin: The minimum margin to accept for an item (after sales tax). Default 20
    - include_hubs: true/false that determines if the other hubs should be considered.
    - daily_value: Minimum daily value sold, using the history, for an item to be considered. Defaults to 1B.
    - optimize: Either 'm3'/'roi'/'profit'. 'm3' is maximizing profit per m3.
    - sales_tax: Default 4%
    """
    # Get query parameters
    hub = request.args.get("hub", "jita").lower()
    capacity = float(request.args.get("capacity", 0))
    shipping = float(request.args.get("shipping", 350))
    max_jumps = int(request.args.get("max_jumps", 5))
    margin = float(request.args.get("margin", 20))
    include_hubs = request.args.get("include_hubs", "false").lower() == "true"
    daily_value = float(request.args.get("daily_value", 1_000_000_000))
    optimize = request.args.get("optimize", "m3")
    sales_tax = float(request.args.get("sales_tax", 0.04))

    if hub not in MAIN_HUBS:
        return jsonify({
            "error": "INVALID_VALUE",
            "details": f"Invalid hub: {hub}"
        }), 400

    if optimize not in ("m3", "roi", "profit"):
        return jsonify({
            "error": "INVALID_VALUE",
            "details": "optimize must be either 'm3', 'roi', or 'profit'"
        }), 400

    sde = data.sde
    if sde is None:
        return jsonify({
            "error": "SDE_NOT_LOADED",
            "details": "SDE data not loaded"
        }), 500

    # Get hub station ID
    hub_station_id = MAIN_HUBS[hub]

    # Get all orders for the hub's market
    hub_orders = data.get_orders_for_market(hub)

    # Get sell orders in the destination hub
    hub_sell_orders = [o for o in hub_orders if not o.is_buy_order]

    # Determine which stations to consider for buying
    # If include_hubs is true, consider all hubs, otherwise just the main hub region
    # We need to consider stations near the hub (within max_jumps)

    # For simplicity, we'll consider all sell orders in the same region as the hub
    # In a real implementation, we'd filter by jumps

    # Group sell orders by station
    station_sell_orders = defaultdict(list)
    for order in hub_sell_orders:
        station_sell_orders[order.location_id].append(order)

    # Filter sell orders by the 4-digit rule and calculate potential profits
    candidates = []

    for station_id, sell_orders in station_sell_orders.items():
        station_name = sde.get_station_name(station_id)
        if not station_name:
            continue

        # Group by type
        type_orders = defaultdict(list)
        for order in sell_orders:
            type_orders[order.type_id].append(order)

        for type_id, ord_list in type_orders.items():
            item_name = sde.get_type_name(type_id)
            if not item_name:
                continue

            # Filter outliers
            filtered = filter_outlier_orders(ord_list, False)
            if not filtered:
                continue

            # Calculate weighted average price (5%)
            weighted_price = calculate_volume_weighted_price(filtered, False)
            if weighted_price <= 0:
                weighted_price = min(o.price for o in filtered)

            # Best price (lowest)
            best_price = min(o.price for o in filtered)
            max_buy_price = get_nearest_price(ord_list, False, best_price)

            if max_buy_price is None:
                continue

            # Get total volume available at this station
            total_volume = sum(o.volume_remain for o in filtered)

            # Get item volume
            item_volume = sde.get_volume(type_id)
            if item_volume <= 0:
                continue

            # Check daily value from volume history
            # Get region for this hub
            hub_region = None
            for region, h in REGION_HUBS.items():
                if h == hub:
                    hub_region = region
                    break

            if hub_region:
                history = data.get_volume_history(type_id, hub_region)
                if history:
                    # Calculate average daily volume
                    # The history has (date, volume) tuples
                    avg_daily_volume = sum(v for _, v in history) / len(history)
                    # Get price from history or use current price
                    daily_value_traded = avg_daily_volume * weighted_price
                else:
                    daily_value_traded = 0
            else:
                daily_value_traded = 0

            # Skip if below daily value threshold
            if daily_value_traded < daily_value:
                continue

            # Get sell orders in destination hub for this item
            dest_sell_orders = [o for o in hub_sell_orders if o.type_id == type_id]
            if not dest_sell_orders:
                continue

            filtered_dest = filter_outlier_orders(dest_sell_orders, False)
            if not filtered_dest:
                continue

            # Calculate sell price (highest buy order in destination)
            dest_buy_orders = [o for o in hub_orders if o.is_buy_order and o.type_id == type_id]
            if not dest_buy_orders:
                continue

            filtered_dest_buy = filter_outlier_orders(dest_buy_orders, True)
            if not filtered_dest_buy:
                continue

            best_buy_price = max(o.price for o in filtered_dest_buy)
            sell_price = get_nearest_price(dest_buy_orders, True, best_buy_price)

            if sell_price is None:
                continue

            # Calculate margin (before tax)
            margin_pct = ((sell_price - max_buy_price) / max_buy_price * 100) if max_buy_price > 0 else 0

            # Check minimum margin (after considering tax)
            # Effective margin after tax: (sell * (1-tax) - buy * (1+tax)) / buy * (1+tax)
            effective_margin = ((sell_price * (1 - sales_tax)) - (max_buy_price * (1 + sales_tax))) / (max_buy_price * (1 + sales_tax)) * 100

            if effective_margin < margin:
                continue

            # Calculate competition in destination hub
            competition = get_competition_metrics(dest_sell_orders, False, 1440)

            # Calculate profit per unit
            profit_per_unit = (sell_price * (1 - sales_tax)) - (max_buy_price * (1 + sales_tax))

            # Calculate quantity that fits in capacity
            max_quantity = int(capacity / item_volume) if capacity > 0 and item_volume > 0 else int(total_volume)

            candidates.append({
                "item": item_name,
                "station": station_name,
                "type_id": type_id,
                "buy_price": max_buy_price,
                "sell_price": sell_price,
                "item_volume": item_volume,
                "available_volume": total_volume,
                "max_quantity": max_quantity,
                "profit_per_unit": profit_per_unit,
                "margin": margin_pct,
                "effective_margin": effective_margin,
                "daily_value": daily_value_traded,
                "competition": competition,
                "shipping_cost_per_m3": shipping
            })

    # Sort candidates based on optimize parameter
    if optimize == "m3":
        # Profit per m3 = profit_per_unit / item_volume
        candidates.sort(key=lambda x: (-(x["profit_per_unit"] / x["item_volume"] if x["item_volume"] > 0 else 0), x["item"]))
    elif optimize == "roi":
        # ROI = profit_per_unit / (buy_price * (1+tax))
        cost_per_unit = candidates[0]["buy_price"] * (1 + sales_tax) if candidates else 1
        candidates.sort(key=lambda x: (-(x["profit_per_unit"] / cost_per_unit * 100) if cost_per_unit > 0 else 0, x["item"]))
    else:  # profit
        candidates.sort(key=lambda x: (-x["profit_per_unit"], x["item"]))

    # Build purchases and sales
    purchases = defaultdict(list)
    sales = {}

    remaining_capacity = capacity
    total_cost = 0.0
    total_revenue = 0.0
    total_profit = 0.0

    for candidate in candidates:
        if remaining_capacity <= 0 and capacity > 0:
            break

        item_name = candidate["item"]
        station = candidate["station"]
        available = candidate["available_volume"]
        max_qty = candidate["max_quantity"]
        item_volume = candidate["item_volume"]
        buy_price = candidate["buy_price"]
        sell_price = candidate["sell_price"]
        profit_unit = candidate["profit_per_unit"]

        # Calculate how many we can buy
        if capacity > 0:
            qty_by_capacity = int(remaining_capacity / item_volume)
            quantity = min(available, max_qty, qty_by_capacity)
        else:
            quantity = min(available, max_qty)

        if quantity <= 0:
            continue

        # Calculate costs with tax
        cost_ex_tax = quantity * buy_price
        cost_with_tax = cost_ex_tax * (1 + sales_tax)
        revenue_with_tax = quantity * sell_price * (1 - sales_tax)
        profit = revenue_with_tax - cost_with_tax

        # Add shipping cost
        shipping_cost = quantity * item_volume * shipping
        profit -= shipping_cost

        # Add to purchases
        purchases[station].append({
            "item": item_name,
            "quantity": quantity,
            "total": round(cost_ex_tax, 2)
        })

        # Track totals
        total_cost += cost_ex_tax
        total_revenue += quantity * sell_price
        total_profit += profit

        # Update remaining capacity
        remaining_capacity -= quantity * item_volume

        # Add to sales (only once per item)
        if item_name not in sales:
            sales[item_name] = {
                "sell_price": sell_price,
                "total": quantity,
                "volume": round(quantity * item_volume, 2),
                "revenue": round(quantity * sell_price, 2),
                "competition": candidate["competition"]
            }

    # Sort purchases by station
    for station in purchases:
        purchases[station].sort(key=lambda x: x["item"])

    return jsonify({
        "cost": round(total_cost, 2),
        "revenue": round(total_revenue, 2),
        "profit": round(total_profit, 2),
        "purchases": dict(purchases),
        "sales": sales
    }), 200


@app.route("/v1/profit/trade", methods=["POST"])
def profit_trade():
    """Find items in a hub to trade inside of that hub.

    Request:
    {
        "hub": <one of the 5 hubs>,
        "margin": <float>,
        "sorting_by": <"ppu" | "margin" | "daily">,
        "k": <int>,
        "daily_traded": <float>,
        "daily_volume": <float>,
        "competition_window": <int>,
        "buy_competition": <int>,
        "sell_competition": <int>,
        "sales_tax": <float>
    }
    """
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

    # Extract parameters with defaults
    hub = data_payload.get("hub", "jita").lower()
    margin = float(data_payload.get("margin", 20))
    sorting_by = data_payload.get("sorting_by", "ppu")
    k = int(data_payload.get("k", 5))
    daily_traded = float(data_payload.get("daily_traded", 100_000_000))
    daily_volume = float(data_payload.get("daily_volume", 5))
    competition_window = int(data_payload.get("competition_window", 1440))
    buy_competition = int(data_payload.get("buy_competition", 3))
    sell_competition = int(data_payload.get("sell_competition", 3))
    sales_tax = float(data_payload.get("sales_tax", 0.04))

    if hub not in MAIN_HUBS:
        return jsonify({
            "error": "INVALID_VALUE",
            "details": f"Invalid hub: {hub}. Must be one of {list(MAIN_HUBS.keys())}"
        }), 400

    if sorting_by not in ("ppu", "margin", "daily"):
        return jsonify({
            "error": "INVALID_VALUE",
            "details": "sorting_by must be either 'ppu', 'margin', or 'daily'"
        }), 400

    sde = data.sde
    if sde is None:
        return jsonify({
            "error": "SDE_NOT_LOADED",
            "details": "SDE data not loaded"
        }), 500

    # Get hub station ID
    hub_station_id = MAIN_HUBS[hub]

    # Get all orders for the hub
    orders = data.get_orders_for_market(hub)

    # Get region for hub (for volume history)
    hub_region = None
    for region, h in REGION_HUBS.items():
        if h == hub:
            hub_region = region
            break

    # Find tradable items (items with both buy and sell orders)
    tradable_items = {}

    # Group orders by type
    type_orders = defaultdict(list)
    for order in orders:
        type_orders[order.type_id].append(order)

    for type_id, ord_list in type_orders.items():
        item_name = sde.get_type_name(type_id)
        if not item_name:
            continue

        # Get buy and sell orders
        buy_orders = [o for o in ord_list if o.is_buy_order]
        sell_orders = [o for o in ord_list if not o.is_buy_order]

        if not buy_orders or not sell_orders:
            continue

        # Filter outliers
        filtered_buy = filter_outlier_orders(buy_orders, True)
        filtered_sell = filter_outlier_orders(sell_orders, False)

        if not filtered_buy or not filtered_sell:
            continue

        # Calculate prices
        best_buy = max(o.price for o in filtered_buy)
        lowest_sell = min(o.price for o in filtered_sell)

        # Calculate 4-digit compliant prices
        buy_price = get_nearest_price(buy_orders, True, best_buy)
        sell_price = get_nearest_price(sell_orders, False, lowest_sell)

        if buy_price is None or sell_price is None:
            continue

        # Calculate margin (before tax)
        margin_pct = ((sell_price - buy_price) / buy_price * 100) if buy_price > 0 else 0

        # Check minimum margin (after tax)
        effective_margin = ((sell_price * (1 - sales_tax)) - (buy_price * (1 + sales_tax))) / (buy_price * (1 + sales_tax)) * 100

        if effective_margin < margin:
            continue

        # Calculate profit per unit (before tax)
        profit_per_unit = sell_price - buy_price

        # Get daily volume from history
        history = data.get_volume_history(type_id, hub_region) if hub_region else []
        if history:
            avg_daily_volume = sum(v for _, v in history) / len(history)
        else:
            avg_daily_volume = 0

        # Get daily value traded
        daily_value_traded = avg_daily_volume * ((best_buy + lowest_sell) / 2)

        # Check thresholds
        if daily_value_traded < daily_traded:
            continue

        if avg_daily_volume < daily_volume:
            continue

        # Calculate competition
        # Buy-side competition: all buy orders in the hub station modified within competition_window
        # Sell-side competition: all sell orders in the hub station modified within competition_window
        comp = get_competition_metrics(ord_list, True, competition_window)
        buy_comp = comp["1_day"] if competition_window >= 1440 else comp.get("1_hour", comp["5_minutes"])

        comp_sell = get_competition_metrics(ord_list, False, competition_window)
        sell_comp = comp_sell["1_day"] if competition_window >= 1440 else comp_sell.get("1_hour", comp_sell["5_minutes"])

        # Check competition limits
        if buy_comp > buy_competition or sell_comp > sell_competition:
            continue

        # Calculate daily profit: 10% of daily volume * ppu * (1 - sales_tax)
        ppu = profit_per_unit * (1 - sales_tax)  # Profit per unit after tax
        daily_profit = avg_daily_volume * 0.10 * ppu

        # Store item data
        tradable_items[item_name] = {
            "margin": round(margin_pct, 2),
            "value": round(daily_value_traded, 2),
            "volume": round(avg_daily_volume, 2),
            "ppu": round(ppu, 2),
            "buy_competition": buy_comp,
            "sell_competition": sell_comp,
            "buy_price": round(buy_price, 2),
            "sell_price": round(sell_price, 2),
            "daily_profit": round(daily_profit, 2)
        }

    # Sort based on sorting_by parameter
    items_list = [(name, data) for name, data in tradable_items.items()]

    if sorting_by == "ppu":
        items_list.sort(key=lambda x: (-x[1]["ppu"], x[0]))
    elif sorting_by == "margin":
        items_list.sort(key=lambda x: (-x[1]["margin"], x[0]))
    else:  # daily
        items_list.sort(key=lambda x: (-x[1]["value"], x[0]))

    # Take top k
    result = {name: data for name, data in items_list[:k]}

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
