#!/usr/bin/env python3
"""Market Tools API - Industrialist market analysis tools with Reprocessing API."""

import gzip
import csv
import json
import secrets
from typing import Any, Dict, Optional, Tuple
from collections import defaultdict

from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.responses import JSONResponse
import uvicorn

app = FastAPI()

price_books: Dict[str, Dict[int, Dict[str, Any]]] = defaultdict(dict)
stations_data: Dict[int, Dict[str, Any]] = {}

type_names: Dict[int, str] = {}
type_names_lower: Dict[str, int] = {}
type_info: Dict[int, Dict[str, Any]] = {}
type_materials: Dict[int, List[Tuple[int, int]]] = {}
groups: Dict[int, Dict[str, Any]] = {}
categories: Dict[int, str] = {}
group_categories: Dict[int, int] = {}
api_keys: Dict[str, Dict[str, Any]] = {}
sde_loaded = False

STRUCTURE_MODIFIERS = {"npc_station": 0.00, "citadel": 0.00, "athanor": 0.02, "tatara": 0.055}
RIG_MODIFIERS = {"null": 0, "t1": 1, "t2": 3}
SECURITY_MODIFIERS = {"highsec": 0.00, "lowsec": 0.06, "nullsec": 0.12}
IMPLANT_MODIFIERS = {"null": 0.00, "RX-801": 0.01, "RX-802": 0.02, "RX-804": 0.04}
MAIN_HUBS = {"jita": 60003760, "amarr": 60008494, "dodixie": 60011866, "rens": 60004588, "hek": 60005686}
HUB_NAMES = {"jita": "Jita", "amarr": "Amarr", "dodixie": "Dodixie", "rens": "Rens", "hek": "Hek"}
DEFAULT_EFFICIENCIES = {"ore": 0.9063, "gas": 0.95, "scrap": 0.55}


def open_any(filepath: str):
    for opener in (
        lambda p: gzip.open(p, 'rt', encoding='utf-8'),
        lambda p: open(p, 'r', encoding='utf-8')
    ):
        try:
            return opener(filepath)
        except Exception:
            pass
    return None


def load_sde(sde_path: str) -> None:
    global type_names, type_names_lower, type_info, type_materials
    global groups, categories, group_categories, sde_loaded

    print(f"Loading SDE from {sde_path}...")
    cache_file = f"{sde_path}/sde_cache.json"
    try:
        with open(cache_file, 'r') as f:
            cache = json.load(f)
        for tid, data in cache.get("types", {}).items():
            tid = int(tid)
            type_names_lower[data["name"].lower()] = tid
            type_info[tid] = {
                "group_id": int(data["group_id"]),
                "volume": float(data["volume"]),
                "portion_size": int(data["portion_size"]),
            }
        for tid, mats in cache.get("materials", {}).items():
            type_materials[int(tid)] = [tuple(m) for m in mats]
        for gid, data in cache.get("groups", {}).items():
            gid = int(gid)
            groups[gid] = {"name": data.get("name", ""), "category_id": int(data.get("category_id", 0))}
            group_categories[gid] = int(data.get("category_id", 0))
        for cid, name in cache.get("categories", {}).items():
            categories[int(cid)] = name
        sde_loaded = True
        print(f"SDE loaded: {len(type_names_lower)} types, {len(type_materials)} materials, {len(groups)} groups")
        return
    except Exception as e:
        print(f"Could not load JSON cache: {e}")

    print("Loading from CSV files...")
    with open_any(f"{sde_path}/invTypes.csv.bz2") as f:
        if f:
            for row in csv.DictReader(f):
                try:
                    tid = int(row['typeID'])
                    name = row['typeName']
                    type_names[tid] = name
                    type_names_lower[name.lower()] = tid
                    type_info[tid] = {
                        "group_id": int(row['groupID']),
                        "volume": float(row['volume']),
                        "portion_size": int(row['portionSize']),
                    }
                except (ValueError, KeyError):
                    continue

    with open_any(f"{sde_path}/invTypeMaterials.csv.bz2") as f:
        if f:
            for row in csv.DictReader(f):
                try:
                    tid = int(row['typeID'])
                    mat_tid = int(row['materialTypeID'])
                    qty = int(row['quantity'])
                    type_materials.setdefault(tid, []).append((mat_tid, qty))
                except (ValueError, KeyError):
                    continue

    with open_any(f"{sde_path}/invGroups.csv.bz2") as f:
        if f:
            for row in csv.DictReader(f):
                try:
                    gid = int(row['groupID'])
                    cid = int(row['categoryID'])
                    groups[gid] = {"name": row.get('groupName', ''), "category_id": cid}
                    group_categories[gid] = cid
                except (ValueError, KeyError):
                    continue

    with open_any(f"{sde_path}/invCategories.csv.bz2") as f:
        if f:
            for row in csv.DictReader(f):
                try:
                    cid = int(row['categoryID'])
                    categories[cid] = row['categoryName']
                except (ValueError, KeyError):
                    continue

    sde_loaded = True
    print(f"SDE loaded: {len(type_names_lower)} types, {len(type_materials)} materials")


def get_type_id(name: str) -> Optional[int]:
    return type_names_lower.get(name.lower())


def get_type_name(type_id: int) -> str:
    return type_names.get(type_id, f"Type {type_id}")


def is_valid_type_id(type_id: int) -> bool:
    return type_id in type_info


def get_item_category(item_tid: int) -> str:
    if item_tid not in type_info:
        return "unknown"
    group_id = type_info[item_tid].get("group_id", 0)
    cat_id = group_categories.get(group_id, 0)
    if cat_id == 25:
        return "ore"
    if group_id in {465, 423, 903}:
        return "ice"
    if group_id in {422, 1033}:
        return "gas"
    return "scrap"


def calculate_yield(structure_config: dict, skills: dict, implant: str) -> dict[str, float]:
    Sm = STRUCTURE_MODIFIERS.get(structure_config.get("type", "npc_station"), 0.00)
    Rm = RIG_MODIFIERS.get(structure_config.get("rig", "null"), 0)
    Sec = SECURITY_MODIFIERS.get(structure_config.get("security", "highsec"), 0.00)
    Im = IMPLANT_MODIFIERS.get(implant, 0.00)
    R = skills.get("Reprocessing", 0)
    Re = skills.get("Reprocessing Efficiency", 0)

    base = 50 + Rm
    mult = base * (1 + Sec) * (1 + Sm) ** 2 * (1 + R * 0.03) * (1 + Re * 0.02)

    Op_gas = skills.get("Gas Cloud Harvesting", 0)
    yields = {
        "ore": round(mult * (1 + skills.get("Ore Processing", 0) * 0.02) * (1 + Im), 2),
        "gas": round(mult * (1 + Op_gas * 0.02) * (1 + Im), 2),
        "scrap": float(min(55, 50 + skills.get("Scrapfield Processing", skills.get("Reprocessing Efficiency", 0)))),
    }
    return yields


def get_efficiency_for_item(item_tid: int, efficiencies: dict) -> float:
    return efficiencies.get(get_item_category(item_tid), DEFAULT_EFFICIENCIES.get(get_item_category(item_tid), 0.5))


def _validate_items(items: dict) -> tuple[dict, dict, dict]:
    unknown, tids, volumes = {}, {}, {}
    for name, qty in items.items():
        tid = get_type_id(name)
        if not tid or tid not in type_info:
            unknown[name] = qty
            continue
        tids[name] = tid
        volumes[name] = qty * float(type_info[tid].get("volume", 0))
    if unknown:
        raise HTTPException(status_code=404, detail={"error": "UNKNOWN_ITEMS", "details": sorted(unknown)})
    return tids, volumes, {}


def reprocess_items(items: dict, efficiencies: dict, drill_down: bool = False, market: str = None) -> dict:
    item_tids, item_volumes, all_products = _validate_items(items)
    input_volume = round(sum(item_volumes.values()), 2)
    processed_outputs = {}
    total_materials = []
    reprocessable_outputs = {}

    for name, tid in item_tids.items():
        qty = items[name]
        portion_size = type_info[tid].get("portion_size", 1)
        num_portions = qty // portion_size
        leftover = qty % portion_size

        if leftover:
            leftover_vol = leftover * float(type_info[tid].get("volume", 0))
            cur_qty, cur_vol = all_products.get(name, (0, 0.0))
            all_products[name] = (cur_qty + leftover, cur_vol + leftover_vol)

        if num_portions > 0 and tid in type_materials:
            for mat_tid, mat_qty in type_materials[tid]:
                total_materials.append((mat_tid, mat_qty * num_portions))
            reprocessable_outputs[name] = num_portions

    final_materials = {}
    for mat_tid, total_qty in total_materials:
        eff = get_efficiency_for_item(mat_tid, efficiencies)
        final_qty = int(total_qty * eff)
        if final_qty > 0:
            final_materials[mat_tid] = final_qty

    for mat_tid, mat_qty in final_materials.items():
        mat_name = get_type_name(mat_tid)
        mat_vol = mat_qty * float(type_info.get(mat_tid, {}).get("volume", 0))
        cur_qty, cur_vol = all_products.get(mat_name, (0, 0.0))
        all_products[mat_name] = (cur_qty + mat_qty, cur_vol + mat_vol)

    if drill_down and reprocessable_outputs:
        drill_outputs = {}
        for mat_name, (qty, _) in all_products.items():
            tid = get_type_id(mat_name)
            if tid and tid in type_materials:
                drill_outputs[mat_name] = qty
        if drill_outputs:
            drill_result = reprocess_items(drill_outputs, efficiencies, drill_down=False, market=None)
            for prod_name, prod_data in drill_result["products"].items():
                new_qty = prod_data["quantity"]
                new_vol = prod_data["volume"]
                all_products[prod_name] = (new_qty, new_vol)

    products = {}
    output_volume = 0.0
    for name, (qty, vol) in all_products.items():
        if qty > 0:
            products[name] = {"quantity": qty, "volume": round(vol, 2), "buy": None, "sell": None}
            output_volume += vol

    if market and market in price_books:
        for name in products:
            tid = get_type_id(name)
            if tid:
                hub_orders = price_books.get(market, {})
                type_orders = [o for o in hub_orders.values() if o.get("type_id") == tid]
                if type_orders:
                    buy_orders = [o for o in type_orders if o.get("is_buy_order")]
                    sell_orders = [o for o in type_orders if not o.get("is_buy_order")]
                    if sell_orders:
                        products[name]["sell"] = round(min(o["price"] for o in sell_orders), 2)
                    if buy_orders:
                        products[name]["buy"] = round(max(o["price"] for o in buy_orders), 2)

    return {
        "inputs": {"volume": input_volume, "buy": None, "sell": None, "buy_5pct": None, "sell_5pct": None},
        "outputs": {"volume": round(output_volume, 2), "buy": None, "sell": None, "buy_5pct": None, "sell_5pct": None},
        "yields": efficiencies.copy(),
        "products": products
    }


def verify_api_key(key: str) -> Optional[dict]:
    return api_keys.get(key)


def generate_api_key(config: dict) -> str:
    key = secrets.token_urlsafe(32)
    api_keys[key] = config
    return key


def _validate_structure(structure: dict) -> Optional[dict]:
    if not structure:
        return None
    if "type" in structure:
        if structure["type"] not in STRUCTURE_MODIFIERS:
            return JSONResponse(status_code=404, content={"code": "INVALID_VALUE", "path": "structure.type", "message": f"Invalid structure type: {structure['type']}"})
        if structure["type"] == "npc_station" and structure.get("rig") is not None:
            return JSONResponse(status_code=404, content={"code": "INVALID_VALUE", "path": "structure.rig", "message": "npc_station cannot specify a rig"})
    if "rig" in structure and structure["rig"] not in RIG_MODIFIERS:
        return JSONResponse(status_code=404, content={"code": "INVALID_VALUE", "path": "structure.rig", "message": f"Invalid rig type: {structure['rig']}"})
    if "security" in structure and structure["security"] not in SECURITY_MODIFIERS:
        return JSONResponse(status_code=404, content={"code": "INVALID_VALUE", "path": "structure.security", "message": f"Invalid security status: {structure['security']}"})
    return None


def _validate_skills(skills: dict) -> Optional[dict]:
    if not skills:
        return None
    for skill, level in skills.items():
        if not isinstance(level, int) or level < 0 or level > 5:
            return JSONResponse(status_code=404, content={"code": "INVALID_VALUE", "path": f"skills.{skill}", "message": f"Skill level must be integer 0-5, got: {level}"})
    return None


@app.post("/v1/config")
async def create_config(request: Request):
    body = await request.json()
    if error := _validate_structure(body.get("structure")):
        return error
    if error := _validate_skills(body.get("skills")):
        return error
    if "implant" in body and body["implant"] not in IMPLANT_MODIFIERS:
        return JSONResponse(status_code=404, content={"code": "INVALID_VALUE", "path": "implant", "message": f"Invalid implant type: {body['implant']}"})

    config = {
        "structure": {"type": body.get("structure", {}).get("type", "npc_station"), "rig": body.get("structure", {}).get("rig"), "security": body.get("structure", {}).get("security", "highsec")},
        "skills": body.get("skills", {}),
        "implant": body.get("implant")
    }
    return JSONResponse(status_code=201, content={"key": generate_api_key(config)})


@app.post("/v1/reprocess")
async def reprocess(request: Request, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    body = await request.json()
    if "items" not in body:
        raise HTTPException(status_code=400, detail={"error": "INVALID_REQUEST", "details": "Missing 'items' field"})
    items = body["items"]
    for name, qty in items.items():
        if not isinstance(qty, int) or qty <= 0:
            raise HTTPException(status_code=400, detail={"error": "INVALID_REQUEST", "details": f"Quantity must be positive integer for item: {name}"})

    if x_api_key and x_api_key in api_keys:
        cfg = api_keys[x_api_key]
        yields = calculate_yield(cfg.get("structure", {"type": "npc_station", "rig": None, "security": "highsec"}), cfg.get("skills", {}), cfg.get("implant"))
    else:
        efficiencies = body.get("efficiency", DEFAULT_EFFICIENCIES.copy())
        for cat in ["ore", "gas", "scrap"]:
            if cat in efficiencies and not 0 <= efficiencies[cat] <= 1:
                raise HTTPException(status_code=400, detail={"error": "INVALID_REQUEST", "details": f"Efficiency must be between 0 and 1 for {cat}"})
        yields = efficiencies

    drill_down = body.get("drill_down", False)
    market = body.get("market")
    if market and market not in {"Jita", "Amarr", "Dodixie", "Rens", "Hek", "null"}:
        market = "null"

    result = reprocess_items(items, yields, drill_down, market)
    return JSONResponse(status_code=201, content=result)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content=exc.detail if isinstance(exc.detail, dict) else {"error": "UNKNOWN_ERROR", "details": str(exc.detail)})


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Market Tools API with Reprocessing")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--address", type=str, default="127.0.0.1")
    parser.add_argument("--sde", type=str, required=True)
    args = parser.parse_args()
    load_sde(args.sde)
    uvicorn.run(app, host=args.address, port=args.port)


if __name__ == "__main__":
    main()
