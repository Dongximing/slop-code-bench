#!/usr/bin/env python3
"""Market Tools API - Deep market tooling for industrialists."""

import argparse
import bz2
import csv
import gzip
import io
import os
import uuid
import yaml
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

import uvicorn
from fastapi import FastAPI, Header, Path, Query, Request
from fastapi.responses import JSONResponse

app = FastAPI()

sde_data: Dict[str, Any] = {}
market_data: Dict[str, Dict[int, List[Dict]]] = {}
station_data: Dict[int, Dict] = {}
all_orders: List[Dict] = []
api_keys: Dict[str, Dict] = {}  # API key storage

# Structure modifiers (Sm)
STRUCTURE_MODIFIERS = {
    "npc_station": 0.0,
    "citadel": 0.0,
    "athanor": 0.02,
    "tatara": 0.055,
}

# Rig modifiers (Rm)
RIG_MODIFIERS = {
    None: 0,
    "t1": 1,
    "t2": 3,
}

# Security modifiers (Sec)
SECURITY_MODIFIERS = {
    "highsec": 0.0,
    "lowsec": 0.06,
    "nullsec": 0.12,
}

# Implant modifiers (Im)
IMPLANT_MODIFIERS = {
    None: 0.0,
    "RX-801": 0.01,
    "RX-802": 0.02,
    "RX-804": 0.04,
}

# Skill type IDs for reprocessing
REPROCESSING_SKILL_ID = 3385
REPROCESSING_EFFICIENCY_SKILL_ID = 3389
SCRAPMETAL_PROCESSING_SKILL_ID = 12196

# Attribute ID for processing skill mapping (attribute 790)
PROCESSING_SKILL_ATTRIBUTE_ID = 790

# Groups for classification
ORE_GROUPS = {450, 451, 452, 453, 454, 455, 456, 457, 458, 459, 460, 461, 462, 467, 468, 469,
              1884, 1911, 1920, 1921, 1922, 1923, 2006, 2022, 2024, 4029, 4030, 4031, 4094,
              4161, 4513, 4514, 4515, 4516, 4568, 4714, 4755, 4756, 4757, 4758, 4759, 4857}
ICE_GROUP = 465
GAS_GROUPS = {711, 4168}  # Harvestable Cloud and Compressed Gas

# Category for ore/ice
ASTEROID_CATEGORY = 25

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

PRICES_COLUMNS = {
    "order_id", "duration", "is_buy_order", "issued", "location_id",
    "min_volume", "price", "range", "system_id", "type_id",
    "volume_remain", "volume_total",
}

STATIONS_COLUMNS = {"location_id", "type", "name"}


def _err(code: int, error: str, details: str) -> JSONResponse:
    return JSONResponse(status_code=code, content={"error": error, "details": details})


def _region_for_order(order: Dict) -> Optional[int]:
    location_id = order.get("location_id")
    if location_id in sde_data["stations"]:
        return sde_data["stations"][location_id]["region_id"]
    if location_id in station_data:
        sys_id = station_data[location_id].get("system_id")
        if sys_id and sys_id in sde_data["systems"]:
            return sde_data["systems"][sys_id]["region_id"]
    return None


def _orders_for_region(orders: List[Dict], region_id: int) -> List[Dict]:
    return [o for o in orders if _region_for_order(o) == region_id]


def _split_buy_sell(orders: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    buys = [o for o in orders if o.get("is_buy_order", False)]
    sells = [o for o in orders if not o.get("is_buy_order", False)]
    return buys, sells


def _filter_outliers(orders: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """Filter outlier buy/sell orders, returning (filtered_buy, filtered_sell)."""
    buys, sells = _split_buy_sell(orders)
    filtered_buy = buys
    filtered_sell = sells

    if buys:
        threshold = max(o["price"] for o in buys) * 0.10
        filtered_buy = [o for o in buys if o["price"] > threshold]

    if sells:
        threshold = min(o["price"] for o in sells) * 10
        filtered_sell = [o for o in sells if o["price"] < threshold]

    return filtered_buy, filtered_sell


def _price_stats(buy_orders: List[Dict], sell_orders: List[Dict]) -> Dict[str, Any]:
    """Compute best price, 5pct price, volume, and order count for buy/sell sides."""
    buy_price = max((o["price"] for o in buy_orders), default=None)
    sell_price = min((o["price"] for o in sell_orders), default=None)
    return {
        "buy": buy_price,
        "sell": sell_price,
        "buy_5pct": calculate_5pct_price(buy_orders, is_buy=True),
        "sell_5pct": calculate_5pct_price(sell_orders, is_buy=False),
        "buy_volume": sum(o["volume_remain"] for o in buy_orders),
        "sell_volume": sum(o["volume_remain"] for o in sell_orders),
        "buy_orders": len(buy_orders),
        "sell_orders": len(sell_orders),
    }


def _location_name(location_id: int) -> str:
    if location_id in station_data:
        return station_data[location_id].get("name", str(location_id))
    if location_id in sde_data["stations"]:
        return sde_data["stations"][location_id].get("name", str(location_id))
    return str(location_id)


def _top_hub_location(orders: List[Dict], region_id: int) -> Optional[int]:
    sell_counts: Dict[int, int] = defaultdict(int)
    for o in _orders_for_region(orders, region_id):
        if not o.get("is_buy_order", False):
            sell_counts[o["location_id"]] += 1
    if not sell_counts:
        return None
    return max(sell_counts, key=sell_counts.get)


# ── SDE Loading ──────────────────────────────────────────────────────────────

def load_sde(sde_dir: str) -> None:
    global sde_data

    sde_data = {
        "types": {},
        "regions": {},
        "systems": {},
        "stations": {},
        "type_attributes": {},
        "type_materials": {},
        "groups": {},
        "categories": {},
        "name_to_type_id": {},
        "portion_size": {},
        "ship_volumes": {},
    }

    def _load_csv(filename: str):
        path = os.path.join(sde_dir, filename)
        if not os.path.exists(path):
            return
        with bz2.open(path, "rt") as f:
            return list(csv.DictReader(f))

    # Load ship volumes from YAML
    ship_volumes_path = os.path.join(sde_dir, "ship_volumes.yaml")
    if os.path.exists(ship_volumes_path):
        with open(ship_volumes_path, "r") as f:
            sde_data["ship_volumes"] = yaml.safe_load(f) or {}

    # Load types
    for row in _load_csv("invTypes.csv.bz2") or []:
        tid = int(row["typeID"])
        name = row["typeName"] or ""
        group_id = int(row["groupID"]) if row["groupID"] else None
        sde_data["types"][tid] = {
            "name": name,
            "group_id": group_id,
            "volume": float(row["volume"]) if row["volume"] else 0.0,
        }
        if name:
            sde_data["name_to_type_id"][name.lower()] = tid
        # Store portion size
        portion_size = int(row["portionSize"]) if row["portionSize"] else 1
        sde_data["portion_size"][tid] = portion_size

    # Load groups
    for row in _load_csv("invGroups.csv.bz2") or []:
        gid = int(row["groupID"])
        sde_data["groups"][gid] = {
            "name": row["groupName"] or "",
            "category_id": int(row["categoryID"]) if row["categoryID"] else None,
        }

    # Load categories
    for row in _load_csv("invCategories.csv.bz2") or []:
        cid = int(row["categoryID"])
        sde_data["categories"][cid] = row["categoryName"] or ""

    for row in _load_csv("mapRegions.csv.bz2") or []:
        sde_data["regions"][int(row["regionID"])] = row["regionName"]

    for row in _load_csv("mapSolarSystems.csv.bz2") or []:
        sid = int(row["solarSystemID"])
        sde_data["systems"][sid] = {
            "region_id": int(row["regionID"]),
            "name": row["solarSystemName"],
        }

    for row in _load_csv("staStations.csv.bz2") or []:
        sid = int(row["stationID"])
        sde_data["stations"][sid] = {
            "system_id": int(row["solarSystemID"]),
            "region_id": int(row["regionID"]),
            "name": row["stationName"],
        }

    # Load type attributes (for processing skill mapping)
    for row in _load_csv("dgmTypeAttributes.csv.bz2") or []:
        tid = int(row["typeID"])
        attr_id = int(row["attributeID"])
        value = row["valueFloat"] if row["valueFloat"] else row["valueInt"]
        if value:
            if tid not in sde_data["type_attributes"]:
                sde_data["type_attributes"][tid] = {}
            try:
                sde_data["type_attributes"][tid][attr_id] = float(value)
            except (ValueError, TypeError):
                pass

    # Load type materials (for reprocessing)
    for row in _load_csv("invTypeMaterials.csv.bz2") or []:
        tid = int(row["typeID"])
        material_tid = int(row["materialTypeID"])
        quantity = int(row["quantity"])
        if tid not in sde_data["type_materials"]:
            sde_data["type_materials"][tid] = {}
        sde_data["type_materials"][tid][material_tid] = quantity


# ── CSV Parsing ──────────────────────────────────────────────────────────────

def parse_iso_datetime(dt_str: str) -> datetime:
    dt_str = dt_str.replace("Z", "+00:00")
    if "+" not in dt_str and "-" not in dt_str[10:]:
        dt_str += "+00:00"
    try:
        return datetime.fromisoformat(dt_str)
    except ValueError:
        return datetime.strptime(dt_str.split("+")[0].split(".")[0], "%Y-%m-%dT%H:%M:%S")


def decompress_and_parse_csv(data: bytes) -> Tuple[List[Dict[str, Any]], Set[str]]:
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

    return list(reader), set(reader.fieldnames)


async def _parse_csv_endpoint(request: Request, required_cols: Set[str]):
    """Read gzip CSV body, validate columns. Returns (rows, columns) or JSONResponse error."""
    try:
        data = await request.body()
    except Exception as e:
        return None, _err(400, "INVALID_FORMAT", f"Failed to read request body: {e}")

    try:
        rows, columns = decompress_and_parse_csv(data)
    except Exception as e:
        return None, _err(400, "INVALID_FORMAT", str(e))

    missing = required_cols - columns
    if missing:
        return None, _err(400, "INVALID_FORMAT", f"Missing required columns: {missing}")

    return rows, None


# ── 5% Price Calculation ─────────────────────────────────────────────────────

def calculate_5pct_price(orders: List[Dict], is_buy: bool) -> Optional[float]:
    """Volume-weighted average price of the top 5% of orders by best price."""
    if not orders:
        return None

    sorted_orders = sorted(orders, key=lambda o: o["price"], reverse=is_buy)

    total_volume = sum(o["volume_remain"] for o in sorted_orders)
    target_volume = total_volume * 0.05

    if target_volume == 0:
        return sorted_orders[0]["price"]

    accumulated = 0.0
    total_value = 0.0

    for order in sorted_orders:
        vol = order["volume_remain"]
        price = order["price"]

        if accumulated + vol >= target_volume:
            total_value += (target_volume - accumulated) * price
            accumulated = target_volume
            break
        total_value += vol * price
        accumulated += vol

    if accumulated == 0:
        return None
    return total_value / accumulated


# ── API Endpoints ────────────────────────────────────────────────────────────

@app.post("/v1/prices")
async def ingest_prices(request: Request):
    global market_data, all_orders

    market = request.query_params.get("market", "jita").lower()
    mode = request.query_params.get("mode", "replace").lower()
    location_filter = request.query_params.get("location_id")

    rows, err = await _parse_csv_endpoint(request, PRICES_COLUMNS)
    if err:
        return err

    parsed_rows = []
    for row in rows:
        try:
            parsed_rows.append({
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
            })
        except (ValueError, KeyError) as e:
            return _err(400, "INVALID_FORMAT", f"Invalid row data: {e}")

    if location_filter:
        lid = int(location_filter)
        parsed_rows = [r for r in parsed_rows if r["location_id"] == lid]

    if market not in market_data or mode == "replace":
        market_data[market] = defaultdict(list)

    order_index: Dict[int, Dict] = {}
    if mode == "append" and market in market_data:
        for orders in market_data[market].values():
            for order in orders:
                order_index[order["order_id"]] = order

    for row in parsed_rows:
        oid = row["order_id"]
        if oid in order_index:
            if row["issued_dt"] > order_index[oid]["issued_dt"]:
                order_index[oid] = row
        else:
            order_index[oid] = row

    new_market_data: Dict[int, List[Dict]] = defaultdict(list)
    for order in order_index.values():
        new_market_data[order["type_id"]].append(order)
    market_data[market] = new_market_data

    all_orders[:] = [o for o in all_orders if o.get("_market") != market]
    for order in order_index.values():
        copy = dict(order)
        copy["_market"] = market
        all_orders.append(copy)

    return JSONResponse(status_code=200, content={"status": "PRICES_UPDATED", "count": len(parsed_rows)})


@app.post("/v1/stations")
async def ingest_stations(request: Request):
    global station_data

    rows, err = await _parse_csv_endpoint(request, STATIONS_COLUMNS)
    if err:
        return err

    count = 0
    for row in rows:
        try:
            location_id = int(row["location_id"])
            station_type = row["type"]
            name = row["name"]

            system_id = None
            if station_type == "Structure":
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
            return _err(400, "INVALID_FORMAT", f"Invalid row data: {e}")

    return JSONResponse(status_code=200, content={"status": "STATIONS_UPDATED", "count": count})


@app.get("/v1/market/{region_id}")
async def get_market_region(
    region_id: int = Path(...),
    type_ids: Optional[str] = Query(None),
    hubs: Optional[int] = Query(None),
):
    region_name = sde_data["regions"].get(region_id)
    if region_name is None:
        return _err(404, "UNKNOWN_ITEMS", f"Unknown region ID: {region_id}")

    region_orders = _orders_for_region(all_orders, region_id)
    hub_location_id = _top_hub_location(all_orders, region_id)

    filtered_buy, filtered_sell = _filter_outliers(region_orders)

    buy_value = sum(o["price"] * o["volume_remain"] for o in filtered_buy)
    sell_value = sum(o["price"] * o["volume_remain"] for o in filtered_sell)

    response: Dict[str, Any] = {
        "name": region_name,
        "sell_orders": len(filtered_sell),
        "buy_orders": len(filtered_buy),
        "sell_value": round(sell_value / 1e9, 2),
        "buy_value": round(buy_value / 1e9, 2),
        "unique_items": len({o["type_id"] for o in filtered_buy + filtered_sell}),
    }

    if type_ids:
        type_id_list = [int(tid.strip()) for tid in type_ids.split(",") if tid.strip()]
        types_response = {}

        for type_id in type_id_list:
            type_info = sde_data["types"].get(type_id)
            if type_info is None:
                return _err(404, "UNKNOWN_ITEMS", f"Unknown type ID: {type_id}")

            hub_orders = [
                o for o in region_orders
                if o["type_id"] == type_id and o["location_id"] == hub_location_id
            ]

            if not hub_orders:
                types_response[type_info["name"]] = {
                    "buy": None, "sell": None, "split": None,
                    "buy_orders": 0, "sell_orders": 0,
                }
                continue

            hub_buys, hub_sells = _split_buy_sell(hub_orders)
            highest_buy = max((o["price"] for o in hub_buys), default=None)
            lowest_sell = min((o["price"] for o in hub_sells), default=None)

            split = None
            if highest_buy is not None and lowest_sell is not None:
                split = (highest_buy + lowest_sell) / 2

            types_response[type_info["name"]] = {
                "buy": highest_buy,
                "sell": lowest_sell,
                "split": split,
                "buy_orders": len(hub_buys),
                "sell_orders": len(hub_sells),
            }

        response["types"] = types_response

    if hubs is not None:
        sell_by_loc: Dict[int, List[Dict]] = defaultdict(list)
        for o in region_orders:
            if not o.get("is_buy_order", False):
                sell_by_loc[o["location_id"]].append(o)

        sorted_locs = sorted(sell_by_loc, key=lambda loc: len(sell_by_loc[loc]), reverse=True)

        response["hubs"] = [
            {
                "station": _location_name(loc_id),
                "orders": len(sell_by_loc[loc_id]),
                "sell_value": round(
                    sum(o["price"] * o["volume_remain"] for o in sell_by_loc[loc_id]) / 1e9, 2
                ),
            }
            for loc_id in sorted_locs[:hubs]
        ]

    return JSONResponse(status_code=200, content=response)


@app.get("/v1/market/{region_id}/{type_id}")
async def get_market_type(
    region_id: int = Path(...),
    type_id: int = Path(...),
):
    type_info = sde_data["types"].get(type_id)
    if type_info is None:
        return _err(404, "UNKNOWN_ITEMS", f"Unknown type ID: {type_id}")

    region_name = sde_data["regions"].get(region_id)
    if region_name is None:
        return _err(404, "UNKNOWN_ITEMS", f"Unknown region ID: {region_id}")

    matching = [
        o for o in all_orders
        if o["type_id"] == type_id and _region_for_order(o) == region_id
    ]

    filtered_buy, filtered_sell = _filter_outliers(matching)

    # Compute thresholds from unfiltered data
    buys_raw, sells_raw = _split_buy_sell(matching)
    buy_threshold = max((o["price"] for o in buys_raw), default=None)
    sell_threshold = min((o["price"] for o in sells_raw), default=None)
    if buy_threshold is not None:
        buy_threshold *= 0.10
    if sell_threshold is not None:
        sell_threshold *= 10

    stats = _price_stats(filtered_buy, filtered_sell)
    sell_5pct = stats["sell_5pct"]
    sell_volume = stats["sell_volume"]

    return JSONResponse(status_code=200, content={
        "name": type_info["name"],
        **stats,
        "buy_threshold": buy_threshold,
        "sell_threshold": sell_threshold,
        "total_value": sell_5pct * sell_volume if sell_5pct is not None else 0.0,
    })


@app.get("/v1/hub-compare/{type_id}")
async def hub_compare(type_id: int = Path(...)):
    type_info = sde_data["types"].get(type_id)
    if type_info is None:
        return _err(404, "UNKNOWN_ITEMS", f"Unknown type ID: {type_id}")

    response = {}
    has_data = False

    for hub_name, hub_info in HUB_STATIONS.items():
        station_id = hub_info["station_id"]
        hub_orders = [
            o for o in all_orders
            if o["type_id"] == type_id and o["location_id"] == station_id
        ]

        if not hub_orders:
            continue

        has_data = True
        filtered_buy, filtered_sell = _filter_outliers(hub_orders)
        stats = _price_stats(filtered_buy, filtered_sell)
        sell_volume = stats["sell_volume"]

        value = None
        if stats["sell_5pct"] is not None and sell_volume > 0:
            value = round((stats["sell_5pct"] * sell_volume) / 1e9, 2)

        response[f"{hub_name}_sell"] = stats["sell"]
        response[f"{hub_name}_buy"] = stats["buy"]
        response[f"{hub_name}_sell_volume"] = sell_volume if sell_volume > 0 else None
        response[f"{hub_name}_buy_volume"] = stats["buy_volume"] if stats["buy_volume"] > 0 else None
        response[f"{hub_name}_value"] = value
        response[f"{hub_name}_sell_5pct"] = stats["sell_5pct"]
        response[f"{hub_name}_buy_5pct"] = stats["buy_5pct"]

    if not has_data:
        return _err(404, "NO_PRICE_DATA", f"No price data found for type ID {type_id} in any hub")

    return JSONResponse(status_code=200, content=response)


# ── Reprocessing Helper Functions ─────────────────────────────────────────────

def get_item_category(type_id: int) -> str:
    """Determine if item is ore, ice, gas, or scrap."""
    type_info = sde_data["types"].get(type_id)
    if not type_info:
        return "scrap"

    group_id = type_info.get("group_id")
    if not group_id:
        return "scrap"

    # Check for ice (group 465)
    if group_id == ICE_GROUP:
        return "ore"  # Ice uses ore efficiency

    # Check for ore (various ore groups in category 25)
    if group_id in ORE_GROUPS:
        return "ore"

    # Check for gas (groups 711 and 4168)
    if group_id in GAS_GROUPS:
        return "gas"

    return "scrap"


def get_item_volume(type_id: int) -> float:
    """Get the volume for an item, using packaged volume for ships."""
    type_info = sde_data["types"].get(type_id)
    if not type_info:
        return 0.0

    group_id = type_info.get("group_id")
    if group_id and group_id in sde_data["groups"]:
        group_info = sde_data["groups"][group_id]
        group_name = group_info.get("name", "")
        # Check if this is a ship group - use packaged volume
        if group_name in sde_data.get("ship_volumes", {}):
            return sde_data["ship_volumes"][group_name]

    return type_info.get("volume", 0.0)


def calculate_yield_from_config(config: Dict) -> Tuple[float, float, float]:
    """Calculate ore, gas, and scrap yields from configuration.
    Returns raw percentage values (not yet rounded, not yet converted to multiplier).
    """
    structure_config = config.get("structure", {})
    skills = config.get("skills", {})
    implant = config.get("implant", None)

    # Structure values
    structure_type = structure_config.get("type", "npc_station")
    rig = structure_config.get("rig", None)
    security = structure_config.get("security", "highsec")

    # Get modifiers
    Sm = STRUCTURE_MODIFIERS.get(structure_type, 0.0)
    Rm = RIG_MODIFIERS.get(rig, 0)
    Sec = SECURITY_MODIFIERS.get(security, 0.0)
    Im = IMPLANT_MODIFIERS.get(implant, 0.0)

    # Skill levels
    R = skills.get("Reprocessing", 0)
    Re = skills.get("Reprocessing Efficiency", 0)

    # Calculate ore/ice yield (percentage, before rounding and before Op)
    # Yield = (50 + Rm) * (1 + Sec) * (1 + Sm) * (1 + (R * 0.03)) * (1 + (Re * 0.02)) * (1 + Im)
    ore_yield_pct = (50 + Rm) * (1 + Sec) * (1 + Sm) * (1 + (R * 0.03)) * (1 + (Re * 0.02)) * (1 + Im)

    # Gas yield uses same formula
    gas_yield_pct = ore_yield_pct

    # Scrap yield: 50 + scrap reprocessing skill (max 55%)
    Op_scrap = skills.get("Scrapmetal Processing", 0)
    scrap_yield_pct = 50 + Op_scrap

    return ore_yield_pct, gas_yield_pct, scrap_yield_pct


def get_processing_skill_level(type_id: int, skills: Dict[str, int]) -> int:
    """Get the applicable processing skill level for an item."""
    # Get the processing skill from attribute 790
    type_attrs = sde_data.get("type_attributes", {}).get(type_id, {})
    processing_skill_id = type_attrs.get(PROCESSING_SKILL_ATTRIBUTE_ID)

    if processing_skill_id:
        # Get skill name from type ID
        skill_info = sde_data["types"].get(int(processing_skill_id), {})
        skill_name = skill_info.get("name", "")
        if skill_name:
            return skills.get(skill_name, 0)

    return 0


def calculate_item_yield(type_id: int, config: Dict) -> float:
    """Calculate yield for a specific item including its processing skill.
    Applies the full yield formula, rounds to 2 decimal places, and returns a multiplier.
    """
    category = get_item_category(type_id)

    if category == "scrap":
        # Scrap uses only 50 + scrap processing skill
        skills = config.get("skills", {})
        Op_scrap = skills.get("Scrapmetal Processing", 0)
        yield_pct = 50 + Op_scrap
        return round(yield_pct, 2) / 100

    # For ore/gas, get base yield percentage and add processing skill bonus
    ore_yield_pct, gas_yield_pct, _ = calculate_yield_from_config(config)

    if category == "gas":
        base_yield_pct = gas_yield_pct
    else:  # ore
        base_yield_pct = ore_yield_pct

    # Add processing skill bonus (2% per level)
    skills = config.get("skills", {})
    Op = get_processing_skill_level(type_id, skills)

    # Yield formula includes (1 + (Op * 0.02))
    final_yield_pct = base_yield_pct * (1 + (Op * 0.02))
    return round(final_yield_pct, 2) / 100


def reprocess_item(type_id: int, quantity: int, efficiency: Dict[str, float], config: Optional[Dict] = None) -> Tuple[Dict[int, int], int]:
    """
    Reprocess an item and return (materials_dict, leftover_quantity).
    materials_dict maps material_type_id -> quantity.
    """
    portion_size = sde_data.get("portion_size", {}).get(type_id, 1)

    # If no portion size or no materials, return empty
    if portion_size <= 0:
        return {}, quantity

    # Get materials for this type
    materials = sde_data.get("type_materials", {}).get(type_id, {})

    if not materials:
        # Not reprocessable
        return {}, quantity

    # Calculate portions
    portions = quantity // portion_size
    leftover = quantity % portion_size

    if portions == 0:
        return {}, quantity  # Can't reprocess, return all as leftover

    # Determine efficiency
    category = get_item_category(type_id)

    if config:
        eff = calculate_item_yield(type_id, config)
    else:
        eff = efficiency.get(category, 0.5)

    # Calculate output materials
    output_materials = {}
    for mat_tid, mat_qty in materials.items():
        total_qty = mat_qty * portions
        result_qty = int(total_qty * eff)  # Round down
        if result_qty > 0:
            output_materials[mat_tid] = result_qty

    return output_materials, leftover


def get_hub_prices(type_id: int, hub_name: str) -> Dict[str, Optional[float]]:
    """Get prices for a type at a specific hub."""
    hub_info = HUB_STATIONS.get(hub_name.lower())
    if not hub_info:
        return {"buy": None, "sell": None, "buy_5pct": None, "sell_5pct": None}

    station_id = hub_info["station_id"]
    hub_orders = [
        o for o in all_orders
        if o["type_id"] == type_id and o["location_id"] == station_id
    ]

    if not hub_orders:
        return {"buy": None, "sell": None, "buy_5pct": None, "sell_5pct": None}

    filtered_buy, filtered_sell = _filter_outliers(hub_orders)
    stats = _price_stats(filtered_buy, filtered_sell)

    return {
        "buy": stats["buy"],
        "sell": stats["sell"],
        "buy_5pct": stats["buy_5pct"],
        "sell_5pct": stats["sell_5pct"],
    }


# ── Reprocessing Endpoints ────────────────────────────────────────────────────

@app.post("/v1/config")
async def create_config(request: Request):
    """Create a configuration and return an API key."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=404,
            content={
                "code": "INVALID_VALUE",
                "path": "body",
                "message": "Invalid JSON body"
            }
        )

    structure_config = body.get("structure", {})
    skills = body.get("skills", {})
    implant = body.get("implant", None)

    # Validate structure type
    structure_type = structure_config.get("type", "npc_station")
    if structure_type not in STRUCTURE_MODIFIERS:
        return JSONResponse(
            status_code=404,
            content={
                "code": "INVALID_VALUE",
                "path": "structure.type",
                "message": f"Invalid structure type: {structure_type}. Must be one of: npc_station, citadel, athanor, tatara"
            }
        )

    # Validate rig
    rig = structure_config.get("rig", None)
    if rig not in RIG_MODIFIERS:
        return JSONResponse(
            status_code=404,
            content={
                "code": "INVALID_VALUE",
                "path": "structure.rig",
                "message": f"Invalid rig: {rig}. Must be null, t1, or t2"
            }
        )

    # NPC station cannot have a rig
    if structure_type == "npc_station" and rig is not None:
        return JSONResponse(
            status_code=404,
            content={
                "code": "INVALID_VALUE",
                "path": "structure.rig",
                "message": "npc_station must have rig=null"
            }
        )

    # Validate security
    security = structure_config.get("security", "highsec")
    if security not in SECURITY_MODIFIERS:
        return JSONResponse(
            status_code=404,
            content={
                "code": "INVALID_VALUE",
                "path": "structure.security",
                "message": f"Invalid security: {security}. Must be one of: highsec, lowsec, nullsec"
            }
        )

    # Validate implant
    if implant not in IMPLANT_MODIFIERS:
        return JSONResponse(
            status_code=404,
            content={
                "code": "INVALID_VALUE",
                "path": "implant",
                "message": f"Invalid implant: {implant}. Must be null, RX-801, RX-802, or RX-804"
            }
        )

    # Validate skills
    for skill_name, skill_level in skills.items():
        if not isinstance(skill_level, int) or skill_level < 0 or skill_level > 5:
            return JSONResponse(
                status_code=404,
                content={
                    "code": "INVALID_VALUE",
                    "path": f"skills.{skill_name}",
                    "message": f"Invalid skill level: {skill_level}. Must be integer 0-5"
                }
            )

    # Create config and generate key
    config = {
        "structure": structure_config,
        "skills": skills,
        "implant": implant,
    }

    api_key = str(uuid.uuid4())
    api_keys[api_key] = config

    return JSONResponse(status_code=201, content={"key": api_key})


@app.post("/v1/reprocess")
async def reprocess(request: Request, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    """Reprocess items and return outputs with pricing."""
    try:
        body = await request.json()
    except Exception:
        return _err(400, "INVALID_FORMAT", "Invalid JSON body")

    items = body.get("items", {})
    efficiency = body.get("efficiency", {
        "ore": 0.9063,
        "gas": 0.95,
        "scrap": 0.55
    })
    drill_down = body.get("drill_down", False)
    market = body.get("market", None)

    # Validate items
    if not items:
        return _err(400, "INVALID_FORMAT", "No items provided")

    # Resolve item names to type IDs
    unknown_items = []
    item_type_ids = {}

    for item_name, quantity in items.items():
        if not isinstance(quantity, int) or quantity <= 0:
            return _err(400, "INVALID_FORMAT", f"Invalid quantity for {item_name}: {quantity}")

        type_id = sde_data.get("name_to_type_id", {}).get(item_name.lower())
        if not type_id:
            unknown_items.append(item_name)
        else:
            item_type_ids[type_id] = quantity

    if unknown_items:
        return JSONResponse(
            status_code=404,
            content={
                "error": "UNKNOWN_ITEMS",
                "details": sorted(unknown_items)
            }
        )

    # Check API key
    config = None
    if x_api_key:
        if x_api_key not in api_keys:
            return JSONResponse(
                status_code=401,
                content={"code": "BAD_API_KEY"}
            )
        config = api_keys[x_api_key]

    # Calculate yields used
    yields_used = {"ore": None, "gas": None, "scrap": None}

    # Calculate input volume and prices
    input_volume = 0.0
    input_buy = 0.0
    input_sell = 0.0
    input_buy_5pct = 0.0
    input_sell_5pct = 0.0

    has_market_data = bool(all_orders) and market is not None

    for type_id, quantity in item_type_ids.items():
        vol = get_item_volume(type_id)
        input_volume += quantity * vol

        if has_market_data:
            prices = get_hub_prices(type_id, market)
            if prices["buy"]:
                input_buy += prices["buy"] * quantity
            if prices["sell"]:
                input_sell += prices["sell"] * quantity
            if prices["buy_5pct"]:
                input_buy_5pct += prices["buy_5pct"] * quantity
            if prices["sell_5pct"]:
                input_sell_5pct += prices["sell_5pct"] * quantity

    # Reprocess items
    products: Dict[int, Dict] = {}  # type_id -> {quantity, volume}

    def add_product(tid: int, qty: int):
        if qty <= 0:
            return
        if tid in products:
            products[tid]["quantity"] += qty
        else:
            vol = get_item_volume(tid)
            products[tid] = {"quantity": qty, "volume": vol}

    # First pass: reprocess all input items
    for type_id, quantity in item_type_ids.items():
        category = get_item_category(type_id)

        # Track yield used
        if config:
            eff = calculate_item_yield(type_id, config)
        else:
            eff = efficiency.get(category, 0.5)

        if yields_used[category] is None or eff < yields_used[category]:
            yields_used[category] = eff

        materials, leftover = reprocess_item(type_id, quantity, efficiency, config)

        # Add leftover to products
        if leftover > 0:
            add_product(type_id, leftover)

        # Add materials to products
        for mat_tid, mat_qty in materials.items():
            add_product(mat_tid, mat_qty)

    # Drill-down reprocessing
    if drill_down:
        changed = True
        while changed:
            changed = False
            new_products = {}

            for tid, prod_info in products.items():
                # Check if this can be reprocessed further
                materials = sde_data.get("type_materials", {}).get(tid, {})
                portion_size = sde_data.get("portion_size", {}).get(tid, 1)

                if materials and portion_size > 0:
                    qty = prod_info["quantity"]
                    portions = qty // portion_size
                    leftover = qty % portion_size

                    if portions > 0:
                        changed = True
                        category = get_item_category(tid)

                        # Track yield used
                        if config:
                            eff = calculate_item_yield(tid, config)
                        else:
                            eff = efficiency.get(category, 0.5)

                        if yields_used[category] is None or eff < yields_used[category]:
                            yields_used[category] = eff

                        # Reprocess
                        for mat_tid, mat_qty in materials.items():
                            total_qty = mat_qty * portions
                            result_qty = int(total_qty * eff)
                            if result_qty > 0:
                                if mat_tid in new_products:
                                    new_products[mat_tid]["quantity"] += result_qty
                                else:
                                    vol = get_item_volume(mat_tid)
                                    new_products[mat_tid] = {"quantity": result_qty, "volume": vol}

                        # Add leftover
                        if leftover > 0:
                            if tid in new_products:
                                new_products[tid]["quantity"] += leftover
                            else:
                                new_products[tid] = {"quantity": leftover, "volume": prod_info["volume"]}
                    else:
                        # Can't reprocess, keep as-is
                        if tid in new_products:
                            new_products[tid]["quantity"] += prod_info["quantity"]
                        else:
                            new_products[tid] = dict(prod_info)
                else:
                    # Not reprocessable, keep as-is
                    if tid in new_products:
                        new_products[tid]["quantity"] += prod_info["quantity"]
                    else:
                        new_products[tid] = dict(prod_info)

            products = new_products

    # Calculate output volume and prices
    output_volume = 0.0
    output_buy = 0.0
    output_sell = 0.0
    output_buy_5pct = 0.0
    output_sell_5pct = 0.0

    products_response = {}
    for tid, prod_info in sorted(products.items(), key=lambda x: sde_data["types"].get(x[0], {}).get("name", "")):
        qty = prod_info["quantity"]
        vol = prod_info["volume"]
        total_vol = round(qty * vol, 2)
        output_volume += total_vol

        type_info = sde_data["types"].get(tid, {})
        name = type_info.get("name", str(tid))

        prod_entry = {
            "quantity": qty,
            "volume": total_vol,
        }

        if has_market_data:
            prices = get_hub_prices(tid, market)
            prod_entry["buy"] = prices["buy"]
            prod_entry["sell"] = prices["sell"]

            if prices["buy"]:
                output_buy += prices["buy"] * qty
            if prices["sell"]:
                output_sell += prices["sell"] * qty
            if prices["buy_5pct"]:
                output_buy_5pct += prices["buy_5pct"] * qty
            if prices["sell_5pct"]:
                output_sell_5pct += prices["sell_5pct"] * qty
        else:
            prod_entry["buy"] = None
            prod_entry["sell"] = None

        products_response[name] = prod_entry

    # Format yields
    yields_response = {}
    for cat in ["ore", "gas", "scrap"]:
        if yields_used[cat] is not None:
            yields_response[cat] = yields_used[cat]
        else:
            # Use default if no items of this category were processed
            if config:
                ore_y_pct, gas_y_pct, scrap_y_pct = calculate_yield_from_config(config)
                defaults_pct = {"ore": ore_y_pct, "gas": gas_y_pct, "scrap": scrap_y_pct}
                yields_response[cat] = round(defaults_pct[cat], 2) / 100
            else:
                yields_response[cat] = efficiency.get(cat, 0.5)

    response = {
        "inputs": {
            "volume": round(input_volume, 2),
            "buy": round(input_buy, 2) if has_market_data else None,
            "sell": round(input_sell, 2) if has_market_data else None,
            "buy_5pct": round(input_buy_5pct, 2) if has_market_data else None,
            "sell_5pct": round(input_sell_5pct, 2) if has_market_data else None,
        },
        "outputs": {
            "volume": round(output_volume, 2),
            "buy": round(output_buy, 2) if has_market_data else None,
            "sell": round(output_sell, 2) if has_market_data else None,
            "buy_5pct": round(output_buy_5pct, 2) if has_market_data else None,
            "sell_5pct": round(output_sell_5pct, 2) if has_market_data else None,
        },
        "yields": yields_response,
        "products": products_response,
    }

    return JSONResponse(status_code=201, content=response)


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Market Tools API")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on")
    parser.add_argument("--address", type=str, default="0.0.0.0", help="Address to bind to")
    parser.add_argument("--sde", type=str, required=True, help="Path to SDE directory")
    args = parser.parse_args()

    print(f"Loading SDE data from {args.sde}...")
    load_sde(args.sde)
    print(f"Loaded {len(sde_data['types'])} types, {len(sde_data['regions'])} regions, "
          f"{len(sde_data['systems'])} systems, {len(sde_data['stations'])} stations")

    uvicorn.run(app, host=args.address, port=args.port)


if __name__ == "__main__":
    main()
