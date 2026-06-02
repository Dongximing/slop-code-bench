#!/usr/bin/env python3
"""Market Tools API - Deep market tooling for industrialists."""

import argparse
import bz2
import csv
import gzip
import io
import os
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

import uvicorn
from fastapi import FastAPI, Path, Query, Request
from fastapi.responses import JSONResponse

app = FastAPI()

sde_data: Dict[str, Any] = {}
market_data: Dict[str, Dict[int, List[Dict]]] = {}
station_data: Dict[int, Dict] = {}
all_orders: List[Dict] = []

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
    }

    def _load_csv(filename: str):
        path = os.path.join(sde_dir, filename)
        if not os.path.exists(path):
            return
        with bz2.open(path, "rt") as f:
            return list(csv.DictReader(f))

    for row in _load_csv("invTypes.csv.bz2") or []:
        tid = int(row["typeID"])
        sde_data["types"][tid] = {
            "name": row["typeName"] or "",
            "group_id": int(row["groupID"]) if row["groupID"] else None,
            "volume": float(row["volume"]) if row["volume"] else 0.0,
        }

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
