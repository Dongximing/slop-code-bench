#!/usr/bin/env python3
"""
Market Tools API - Industrialist market analysis tools
With Reprocessing API
"""

import gzip
import csv
import json
import base64
import secrets
import hmac
import hashlib
from typing import Any, Dict, List, Optional, Tuple
from collections import defaultdict
import io
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Query, Request, Header
from fastapi.responses import JSONResponse
import uvicorn

app = FastAPI()

# In-memory data stores
price_books: Dict[str, Dict[int, Dict[str, Any]]] = defaultdict(dict)  # market -> order_id -> order_data
stations_data: Dict[int, Dict[str, Any]] = {}  # location_id -> station_data

# SDE data
type_names: Dict[int, str] = {}
type_names_lower: Dict[str, int] = {}  # lowercase name -> type_id
type_info: Dict[int, Dict[str, Any]] = {}  # type_id -> {group_id, volume, portion_size}
type_materials: Dict[int, List[Tuple[int, int]]] = {}  # type_id -> [(material_id, quantity), ...]
groups: Dict[int, Dict[str, Any]] = {}  # group_id -> {name, category_id}
categories: Dict[int, str] = {}  # category_id -> name
group_categories: Dict[int, int] = {}  # group_id -> category_id

# Reprocessing configuration store (API keys -> config)
api_keys: Dict[str, Dict[str, Any]] = {}

# Structure modifiers
STRUCTURE_MODIFIERS = {
    "npc_station": {"Sm": 0.00},
    "citadel": {"Sm": 0.00},
    "athanor": {"Sm": 0.02},
    "tatara": {"Sm": 0.055}
}

RIG_MODIFIERS = {
    "null": 0,
    "t1": 1,
    "t2": 3
}

SECURITY_MODIFIERS = {
    "highsec": 0.00,
    "lowsec": 0.06,
    "nullsec": 0.12
}

# Implant modifiers
IMPLANT_MODIFIERS = {
    "null": 0.00,
    "RX-801": 0.01,
    "RX-802": 0.02,
    "RX-804": 0.04
}

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

# Default efficiencies
DEFAULT_EFFICIENCIES = {
    "ore": 0.9063,
    "gas": 0.95,
    "scrap": 0.55
}


def open_any(filepath: str):
    """Open as bz2, gzip, or plain text."""
    for opener in (lambda p: gzip.open(p, 'rt', encoding='utf-8'),
                   lambda p: gzip.open(p, 'rt', encoding='utf-8'),
                   lambda p: open(p, 'r', encoding='utf-8')):
        try:
            return opener(filepath)
        except Exception:
            pass
    return None


def load_sde(sde_path: str) -> None:
    """Load SDE data including reprocessing-related data."""
    global type_names, type_names_lower, type_info, type_materials
    global groups, categories, group_categories, sde_loaded

    print(f"Loading SDE from {sde_path}...")

    # Try to load JSON cache first
    cache_file = f"{sde_path}/sde_cache.json"
    try:
        with open(cache_file, 'r') as f:
            cache = json.load(f)
        print("Loading from JSON cache...")

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

    # Fallback to loading from CSVs directly
    print("Loading from CSV files...")

    # Load invTypes.csv.bz2
    with open_any(f"{sde_path}/invTypes.csv.bz2") as f:
        if f:
            reader = csv.DictReader(f)
            for row in reader:
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

    # Load invTypeMaterials.csv.bz2
    with open_any(f"{sde_path}/invTypeMaterials.csv.bz2") as f:
        if f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    tid = int(row['typeID'])
                    mat_tid = int(row['materialTypeID'])
                    qty = int(row['quantity'])
                    if tid not in type_materials:
                        type_materials[tid] = []
                    type_materials[tid].append((mat_tid, qty))
                except (ValueError, KeyError):
                    continue

    # Load invGroups.csv.bz2
    with open_any(f"{sde_path}/invGroups.csv.bz2") as f:
        if f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    gid = int(row['groupID'])
                    cid = int(row['categoryID'])
                    groups[gid] = {"name": row.get('groupName', ''), "category_id": cid}
                    group_categories[gid] = cid
                except (ValueError, KeyError):
                    continue

    # Load invCategories.csv.bz2
    with open_any(f"{sde_path}/invCategories.csv.bz2") as f:
        if f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    cid = int(row['categoryID'])
                    categories[cid] = row['categoryName']
                except (ValueError, KeyError):
                    continue

    sde_loaded = True
    print(f"SDE loaded: {len(type_names_lower)} types, {len(type_materials)} materials")


def get_type_id(name: str) -> Optional[int]:
    """Get type ID from name (case-insensitive)."""
    return type_names_lower.get(name.lower())


def get_type_name(type_id: int) -> str:
    """Get type name from ID."""
    return type_names.get(type_id, f"Type {type_id}")


def is_valid_type_id(type_id: int) -> bool:
    """Check if type ID exists in SDE."""
    return type_id in type_info


def get_item_category(item_tid: int) -> str:
    """Determine item category for reprocessing efficiency."""
    if item_tid not in type_info:
        return "unknown"

    group_id = type_info[item_tid].get("group_id", 0)

    # Check if ore category (Asteroid = 25)
    cat_id = group_categories.get(group_id, 0)
    if cat_id == 25:  # Asteroid category
        return "ore"

    # Check ice groups (465, 423, etc.)
    if group_id in {465, 423, 903}:
        return "ice"

    # Check gas groups
    if group_id in {422, 1033}:
        return "gas"

    # Everything else is scrap
    return "scrap"


def calculate_yield(structure_config: Dict[str, Any], skills: Dict[str, int], implant: str) -> Dict[str, float]:
    """Calculate reprocessing yields for ore/ice/gas based on configuration."""
    yields = {}

    # Get structure and security modifiers
    struct_type = structure_config.get("type", "npc_station")
    rig = structure_config.get("rig", "null")
    security = structure_config.get("security", "highsec")

    Sm = STRUCTURE_MODIFIERS.get(struct_type, STRUCTURE_MODIFIERS["npc_station"])["Sm"]
    Rm = RIG_MODIFIERS.get(rig, 0)
    Sec = SECURITY_MODIFIERS.get(security, 0.00)
    Im = IMPLANT_MODIFIERS.get(implant, 0.00)

    # Get skills
    R = skills.get("Reprocessing", 0)
    Re = skills.get("Reprocessing Efficiency", 0)

    # Find ore processing skill from item attributes (we'll use Ore Processing as default)
    Op = skills.get("Ore Processing", 0)  # This could be adjusted per item

    # Calculate yields for each category
    for item_type in ["ore", "gas"]:
        # Yield = (50 + Rm) * (1 + Sec) * (1 + Sm) * (1 + Sm) * (1 + R * 0.03) * (1 + Re * 0.02) * (1 + Op * 0.02) * (1 + Im)
        # Wait, let me re-read the specification
        # Yield = (50 + Rm) * (1 + Sec) * (1 + Sm) * (1 + Sm) * (1 + R * 0.03) * (1 + Re * 0.02) * (1 + Op * 0.02) * (1 + Im)
        # That seems like Sm is duplicated. Let me check the spec again...
        # The spec says: (1 + Sec) * (1 + Sm) * (1 + Sm) - yes, Sm appears twice!

        base = 50 + Rm  # For scrap, Rm is different, let's handle that below

        # For ore and gas, calculate with all modifiers
        # But for scrap, it's just 50 + scrap reprocessing skill (max 55)
        yield_mult = base * (1 + Sec) * (1 + Sm) * (1 + Sm) * (1 + R * 0.03) * (1 + Re * 0.02) * (1 + Op * 0.02) * (1 + Im)
        yield_percent = round(yield_mult, 2)

        if item_type == "gas":
            # Use Gas Processing skill for gas
            Op_gas = skills.get("Gas Cloud Harvesting", 0)
            yield_mult_gas = base * (1 + Sec) * (1 + Sm) * (1 + Sm) * (1 + R * 0.03) * (1 + Re * 0.02) * (1 + Op_gas * 0.02) * (1 + Im)
            yields["gas"] = round(yield_mult_gas, 2)
        else:
            yields["ore"] = yield_percent

    # Scrap yield: 50 + scrap reprocessing skill (max 55)
    # The specification says "Scrap reprocessing only gets 50 + scrap reprocessing skill for max of 55"
    scrap_skill = skills.get("Scrapfield Processing", skills.get("Reprocessing Efficiency", 0))
    scrap_yield = min(55, 50 + scrap_skill)
    yields["scrap"] = float(scrap_yield)

    return yields


def get_efficiency_for_item(item_tid: int, efficiencies: Dict[str, float]) -> float:
    """Get the appropriate efficiency multiplier for an item."""
    category = get_item_category(item_tid)
    return efficiencies.get(category, DEFAULT_EFFICIENCIES.get(category, 0.5))


def reprocess_items(items: Dict[str, int],
                    efficiencies: Dict[str, float],
                    drill_down: bool = False,
                    market: Optional[str] = None) -> Dict[str, Any]:
    """
    Perform reprocessing calculation.

    Args:
        items: Dict mapping item name to quantity
        efficiencies: Dict with 'ore', 'gas', 'scrap' efficiency values
        drill_down: Whether to reprocess outputs recursively
        market: Market hub for pricing, or None for no pricing

    Returns:
        Dict with inputs, outputs, yields, and products
    """
    # Validate all items first
    unknown_items = []
    item_tids = {}
    item_volumes = {}

    for name, qty in items.items():
        tid = get_type_id(name)
        if tid is None or tid not in type_info:
            unknown_items.append(name)
            continue
        item_tids[name] = tid
        item_volumes[name] = qty * float(type_info[tid].get("volume", 0))

    if unknown_items:
        raise HTTPException(
            status_code=404,
            detail={"error": "UNKNOWN_ITEMS", "details": sorted(unknown_items)}
        )

    # Calculate input volume
    input_volume = round(sum(item_volumes.values()), 2)

    # Initialize tracking
    all_products: Dict[str, Tuple[int, float]] = defaultdict(lambda: (0, 0.0))  # name -> (quantity, volume)
    processed_outputs: Dict[str, Tuple[int, float]] = {}  # For drill-down: processed items

    # Get portion sizes and process
    total_output_materials: List[Tuple[int, int]] = []  # Material ID -> total quantity across all portions

    # Track what was reprocessed (for drill-down)
    reprocessable_outputs: Dict[str, int] = defaultdict(int)  # name -> remaining quantity

    # First pass: process all items and collect material outputs
    for name, tid in item_tids.items():
        qty = items[name]
        portion_size = type_info[tid].get("portion_size", 1)
        efficiency = get_efficiency_for_item(tid, efficiencies)

        # Calculate number of full portions
        num_portions = qty // portion_size
        leftover = qty % portion_size

        # Add leftover to products (not reprocessed)
        if leftover > 0:
            leftover_volume = leftover * float(type_info[tid].get("volume", 0))
            current_qty, current_vol = all_products[name]
            all_products[name] = (current_qty + leftover, current_vol + leftover_volume)

        # Process full portions if any
        if num_portions > 0 and tid in type_materials:
            # Sum materials from all processed portions
            for mat_tid, mat_qty in type_materials[tid]:
                total_output_materials.append((mat_tid, mat_qty * num_portions))
            # Mark as processed for drill-down
            reprocessable_outputs[name] = num_portions

    # Apply efficiency to all materials (rounding down after aggregation)
    final_materials: Dict[int, int] = {}
    for mat_tid, total_qty in total_output_materials:
        category = get_item_category(mat_tid)
        eff = get_efficiency_for_item(mat_tid, efficiencies)
        final_qty = int(total_qty * eff)  # Round down
        if final_qty > 0:
            final_materials[mat_tid] = final_qty

    # Add materials to products
    for mat_tid, mat_qty in final_materials.items():
        mat_name = get_type_name(mat_tid)
        mat_volume = mat_qty * float(type_info.get(mat_tid, {}).get("volume", 0))
        current_qty, current_vol = all_products[mat_name]
        all_products[mat_name] = (current_qty + mat_qty, current_vol + mat_volume)

    # Drill-down: recursively reprocess reprocessable outputs
    if drill_down and reprocessable_outputs:
        # This is simplified - in a full implementation, we'd need to track what items
        # were generated from reprocessing and check their portion sizes
        # For now, we'll apply the efficiency again to the materials we generated

        # Track items that are reprocessable (have materials)
        drill_outputs: Dict[str, int] = defaultdict(int)
        for mat_name, qty, _ in [(n, p[0], p[1]) for n, p in all_products.items()]:
            mat_tid = get_type_id(mat_name)
            if mat_tid and mat_tid in type_materials:
                # This is reprocessable
                drill_outputs[mat_name] = qty

        if drill_outputs:
            # Process these as if they were new inputs, but using same efficiencies
            # We'll call reprocess_items recursively for these
            drill_result = reprocess_items(drill_outputs, efficiencies, drill_down=False, market=None)

            # Add the drill-down outputs to the product list (overwriting the raw materials)
            for prod_name, prod_data in drill_result["products"].items():
                new_qty = prod_data["quantity"]
                new_vol = prod_data["volume"]
                current_qty, current_vol = all_products[prod_name]
                # Replace the material with the drill-down output (they become new items)
                all_products[prod_name] = (new_qty, new_vol)

    # Finalize products
    products = {}
    output_volume = 0.0

    for name, (qty, vol) in all_products.items():
        if qty > 0:
            products[name] = {
                "quantity": qty,
                "volume": round(vol, 2),
                "buy": None,
                "sell": None
            }
            output_volume += vol

    # Get market prices if market is specified
    if market and market in price_books:
        for name in products:
            tid = get_type_id(name)
            if tid:
                # Get buy/sell prices for this type
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
        "inputs": {
            "volume": input_volume,
            "buy": None,
            "sell": None,
            "buy_5pct": None,
            "sell_5pct": None
        },
        "outputs": {
            "volume": round(output_volume, 2),
            "buy": None,
            "sell": None,
            "buy_5pct": None,
            "sell_5pct": None
        },
        "yields": efficiencies.copy(),
        "products": products
    }


def verify_api_key(key: str) -> Optional[Dict[str, Any]]:
    """Verify and decode an API key."""
    return api_keys.get(key)


def generate_api_key(config: Dict[str, Any]) -> str:
    """Generate a secure API key encoding the configuration."""
    # Create a unique key
    key = secrets.token_urlsafe(32)
    api_keys[key] = config
    return key


@app.post("/v1/config")
async def create_config(request: Request):
    """Create a reusable API key that encodes structure/skill/implant inputs."""
    body = await request.json()

    # Validate structure config
    if "structure" in body:
        structure = body["structure"]

        if "type" in structure:
            if structure["type"] not in STRUCTURE_MODIFIERS:
                return JSONResponse(
                    status_code=404,
                    content={
                        "code": "INVALID_VALUE",
                        "path": "structure.type",
                        "message": f"Invalid structure type: {structure['type']}"
                    }
                )
            if structure["type"] == "npc_station" and "rig" in structure and structure["rig"] is not None:
                return JSONResponse(
                    status_code=404,
                    content={
                        "code": "INVALID_VALUE",
                        "path": "structure.rig",
                        "message": "npc_station cannot specify a rig"
                    }
                )

        if "rig" in structure and structure["rig"] is not None:
            if structure["rig"] not in RIG_MODIFIERS:
                return JSONResponse(
                    status_code=404,
                    content={
                        "code": "INVALID_VALUE",
                        "path": "structure.rig",
                        "message": f"Invalid rig type: {structure['rig']}"
                    }
                )

        if "security" in structure:
            if structure["security"] not in SECURITY_MODIFIERS:
                return JSONResponse(
                    status_code=404,
                    content={
                        "code": "INVALID_VALUE",
                        "path": "structure.security",
                        "message": f"Invalid security status: {structure['security']}"
                    }
                )

    # Validate skills
    if "skills" in body:
        for skill, level in body["skills"].items():
            if not isinstance(level, int) or level < 0 or level > 5:
                return JSONResponse(
                    status_code=404,
                    content={
                        "code": "INVALID_VALUE",
                        "path": f"skills.{skill}",
                        "message": f"Skill level must be integer 0-5, got: {level}"
                    }
                )

    # Validate implant
    if "implant" in body:
        if body["implant"] not in IMPLANT_MODIFIERS:
            return JSONResponse(
                status_code=404,
                content={
                    "code": "INVALID_VALUE",
                    "path": "implant",
                    "message": f"Invalid implant type: {body['implant']}"
                }
            )

    # Set defaults
    config = {
        "structure": {
            "type": body.get("structure", {}).get("type", "npc_station"),
            "rig": body.get("structure", {}).get("rig", None),
            "security": body.get("structure", {}).get("security", "highsec")
        },
        "skills": body.get("skills", {}),
        "implant": body.get("implant", None)
    }

    key = generate_api_key(config)

    return JSONResponse(
        status_code=201,
        content={"key": key}
    )


@app.post("/v1/reprocess")
async def reprocess(
    request: Request,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key")
):
    """Reprocess items and calculate outputs."""
    body = await request.json()

    if "items" not in body:
        raise HTTPException(
            status_code=400,
            detail={"error": "INVALID_REQUEST", "details": "Missing 'items' field"}
        )

    items = body["items"]

    # Validate items
    for name, qty in items.items():
        if not isinstance(qty, int) or qty <= 0:
            raise HTTPException(
                status_code=400,
                detail={"error": "INVALID_REQUEST", "details": f"Quantity must be positive integer for item: {name}"}
            )

    # Determine efficiency values
    if x_api_key and x_api_key in api_keys:
        # Use configuration-driven yields
        config = api_keys[x_api_key]
        skills = config.get("skills", {})
        structure = config.get("structure", {"type": "npc_station", "rig": None, "security": "highsec"})
        implant = config.get("implant", None)

        yields = calculate_yield(structure, skills, implant)
    else:
        # Use provided efficiency values
        efficiencies = body.get("efficiency", DEFAULT_EFFICIENCIES.copy())

        # Validate efficiency values
        for cat in ["ore", "gas", "scrap"]:
            if cat in efficiencies:
                if not 0 <= efficiencies[cat] <= 1:
                    raise HTTPException(
                        status_code=400,
                        detail={"error": "INVALID_REQUEST", "details": f"Efficiency must be between 0 and 1 for {cat}"}
                    )
        yields = efficiencies

    # Get optional parameters
    drill_down = body.get("drill_down", False)
    market = body.get("market")

    if market and market not in {"Jita", "Amarr", "Dodixie", "Rens", "Hek", "null"}:
        market = "null"

    # Perform reprocessing
    result = reprocess_items(items, yields, drill_down, market)

    return JSONResponse(status_code=201, content=result)


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

    parser = argparse.ArgumentParser(description="Market Tools API with Reprocessing")
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
