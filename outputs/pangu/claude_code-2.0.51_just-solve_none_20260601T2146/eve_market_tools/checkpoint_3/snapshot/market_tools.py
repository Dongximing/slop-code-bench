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

# Mineral type IDs
MINERAL_IDS = {34: "Tritanium", 35: "Pyerite", 36: "Mexallon", 37: "Isogen", 38: "Nocxium", 39: "Zydrine", 40: "Megacyte"}
MINERAL_NAME_TO_ID = {v: k for k, v in MINERAL_IDS.items()}

# Ore groups from SDE
ORE_GROUPS = {450, 451, 452, 453, 454, 455, 456, 457, 458, 459, 460, 461, 462, 467, 468, 469}

# Variants mapping: ore name -> variant multiplier
VARIANT_MAP = {
    "": 1.0,       # base
    "Condensed": 1.05,   # +5%
    "Massive": 1.10,     # +10%
    "Glossy": 1.15,      # +15%
    "Crimson": 1.03,     # +3% variants (not commonly used)
    "Prime": 1.06,
    "Triclinic": 1.03,
    "Monoclinic": 1.06,
    "Sharp": 1.05,
    "Crystalline": 1.08,
    "Obsidian": 1.04,
    "Onyx": 1.03,
    "Vitric": 1.03,
    "Glazed": 1.06,
    "Vivid": 1.04,
    "Radiant": 1.08,
    "Pure": 1.03,
    "Pristine": 1.06,
    "Fiery": 1.03,
    "Luminous": 1.06,
    "Azure": 1.03,
    "Rich": 1.06,
    "Solid": 1.03,
    "Viscous": 1.06,
    "Bright": 1.04,
    "Gleaming": 1.08,
    "Iridescent": 1.05,
    "Prismatic": 1.10,
    "Silvery": 1.03,
    "Golden": 1.06,
    "Magma": 1.03,
    "Vitreous": 1.06,
}


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


# ===== COMPRESSION ENDPOINT =====

def get_ore_candidates(efficiencies: dict, ore_variants: dict) -> list:
    """
    Build a list of ore candidates that can be compressed.
    Returns list of dicts with:
      - type_id, name, volume, portion_size, materials (list of [tid, qty])
      - effective_yield: dict of mineral_id -> effective yield per portion
      - is_ore or is_gas category
      - variant_mult
    """
    candidates = []

    for tid, name in type_names.items():
        if tid not in type_info:
            continue
        info = type_info[tid]
        gid = info.get("group_id", 0)
        volume = info.get("volume", 0)
        portion_size = info.get("portion_size", 1)
        materials = type_materials.get(tid, [])

        # Only consider items with materials and portion size
        if not materials or portion_size <= 0 or volume <= 0:
            continue

        # Determine category
        is_ore = gid in ORE_GROUPS
        is_gas = gid in {422, 1033}  # Gas groups
        if not (is_ore or is_gas):
            continue

        # Determine variant multiplier
        variant_mult = 1.0  # default base
        variant_key = None
        for key in ["Condensed", "Massive", "Glossy", "Crimson", "Prime",
                     "Triclinic", "Monoclinic", "Sharp", "Crystalline",
                     "Obsidian", "Onyx", "Vitric", "Glazed", "Vivid",
                     "Radiant", "Pure", "Pristine", "Fiery", "Luminous",
                     "Azure", "Rich", "Solid", "Viscous", "Bright",
                     "Gleaming", "Iridescent", "Prismatic", "Silvery",
                     "Golden", "Magma", "Vitreous"]:
            if key in name:
                variant_key = key
                variant_mult = VARIANT_MAP[key]
                break

        # Check if this variant is enabled
        if variant_key is None:
            if not ore_variants.get("base", False):
                continue
        elif variant_key == "Condensed" and not ore_variants.get("plus5", False):
            continue
        elif variant_key == "Massive" and not ore_variants.get("plus10", False):
            continue
        elif variant_key == "Glossy" and not ore_variants.get("plus15", False):
            continue
        elif variant_key in {"Crimson", "Prime", "Triclinic", "Monoclinic",
                              "Sharp", "Crystalline", "Obsidian", "Onyx"}:
            # These are +3% variants, treat as base for simplicity or skip if base is disabled
            if not ore_variants.get("base", False):
                continue
        elif variant_key in {"Vitric", "Glazed", "Vivid", "Radiant", "Pure",
                              "Pristine", "Fiery", "Luminous", "Azure", "Solid",
                              "Viscous", "Bright", "Silvery", "Magma"}:
            # +3% variants
            if not ore_variants.get("base", False):
                continue
        elif variant_key in {"Gleaming", "Iridescent", "Rich", "Viscous"}:
            # +5% to +6%
            if not (ore_variants.get("base", False) or ore_variants.get("plus5", False)):
                continue
        elif variant_key in {"Prismatic", "Golden"}:
            # +10% or +6%
            if not (ore_variants.get("base", False) or ore_variants.get("plus5", False) or ore_variants.get("plus10", False)):
                continue

        # Get efficiency for this category
        cat = "ore" if is_ore else "gas"
        eff = efficiencies.get(cat, DEFAULT_EFFICIENCIES.get(cat, 0.5))

        # Calculate effective yields per portion
        effective_yields = {}
        for mat_tid, mat_qty in materials:
            effective_yields[mat_tid] = int(mat_qty * variant_mult * eff)

        candidates.append({
            "type_id": tid,
            "name": name,
            "volume": volume,
            "portion_size": portion_size,
            "materials": materials,  # base quantities
            "effective_yields": effective_yields,
            "is_ore": is_ore,
            "is_gas": is_gas,
            "variant_mult": variant_mult,
        })

    return candidates


def compress_request(req_body: dict) -> dict:
    """
    Main optimization function for /v1/compress endpoint.
    Implements the compression optimizer with cost minimization.
    """
    global price_books, stations_data, type_names_lower, type_info

    # Parse request
    materials_target = req_body.get("materials", {})
    hubs = [h.lower() for h in req_body.get("hubs", [])]
    cost_per_m3 = req_body.get("cost_per_m3", 350.0)
    collateral_tax = req_body.get("collateral_tax", 0.0)
    efficiencies = req_body.get("efficiencies", DEFAULT_EFFICIENCIES.copy())
    ore_variants = req_body.get("ore_variants", {"base": True, "plus5": True, "plus10": True, "plus15": True})

    # Validate and normalize targets
    material_tids = {}
    unknown_materials = []
    for name, qty in materials_target.items():
        if not isinstance(qty, int) or qty <= 0:
            raise HTTPException(status_code=400, detail={"error": "INVALID_REQUEST", "details": f"Quantity must be positive integer for: {name}"})
        tid = MINERAL_NAME_TO_ID.get(name)
        if tid is None:
            unknown_materials.append(name)
        else:
            material_tids[tid] = qty

    if unknown_materials:
        raise HTTPException(status_code=404, detail={"error": "UNKNOWN_ITEMS", "details": sorted(unknown_materials)})

    if not hubs:
        raise HTTPException(status_code=400, detail={"error": "INVALID_REQUEST", "details": "Missing 'hubs' field"})

    # Validate hubs
    valid_hubs = set(MAIN_HUBS.keys())
    for hub in hubs:
        if hub not in valid_hubs:
            raise HTTPException(status_code=400, detail={"error": "INVALID_REQUEST", "details": f"Invalid hub: {hub}. Valid hubs: {list(valid_hubs)}"})

    # Get ore candidates based on SDE and variant filters
    candidates = get_ore_candidates(efficiencies, ore_variants)

    if not candidates:
        raise HTTPException(status_code=404, detail={"error": "NO_CANDIDATES", "details": "No ore candidates found matching the specified filters"})

    # Build ore-to-materials mapping for quick lookup
    mineral_candidates = {}  # mineral_tid -> list of ore candidates
    for c in candidates:
        for mat_tid, eff_yield in c["effective_yields"].items():
            if mat_tid in MINERAL_IDS:
                if mat_tid not in mineral_candidates:
                    mineral_candidates[mat_tid] = []
                mineral_candidates[mat_tid].append(c)

    # Build buy orders index for the requested hubs
    # price_books: Dict[station_name, Dict[type_id, {order_data...}]]
    hub_station_ids = {h: MAIN_HUBS[h] for h in hubs if h in MAIN_HUBS}
    hub_name_to_station = {}
    for loc_id, data in stations_data.items():
        name = data.get("name", "")
        for hub_key, station_id in hub_station_ids.items():
            if loc_id == station_id:
                hub_name_to_station[hub_key] = name
                break

    # For each hub, build a mapping of ore_tid -> best sell orders (sorted by price ascending)
    # We need available volumes too
    hub_orders = {}  # hub_name -> { ore_type_id -> [(price, volume_available, station_name), ...] }
    for hub_key in hubs:
        hub_orders[hub_key] = {}
        if hub_key not in price_books:
            continue
        hub_price_book = price_books[hub_key]
        for tid, orders in hub_price_book.items():
            if orders.get("is_buy_order"):
                continue  # We want sell orders
            # Group by location_id to get volumes per station
            # orders structure from price_books: typically dict of order_id -> order
            sell_orders = []
            for order_id, order in orders.items():
                # Skip if already a sell order check
                pass

        # Re-parse: each price_book entry might be {type_id: {order_id: {price, volume_remain, location_id, is_buy_order}}}
        # or simplified. Let me check the actual structure by examining how it's populated.
        # Looking at test_api.py, orders have: type_id, price, volume_remain, is_buy_order, location_id

    # Build better hub_orders structure
    for hub_key in hubs:
        hub_orders[hub_key] = {}
        if hub_key not in price_books:
            continue
        hub_book = price_books[hub_key]
        # hub_book might be keyed by type_id -> order_id -> order
        # or directly by order_id. Let's handle both.
        for key, val in hub_book.items():
            if isinstance(val, dict) and "type_id" in val:
                # This is an order entry keyed by order_id
                # Actually, looking at reprocess_items, hub_orders[type_id] gives orders dict
                # Let me check the structure used in the code...
                pass

    # Simpler approach: iterate through all orders in price_books[hub_key]
    for hub_key in hubs:
        hub_orders[hub_key] = {}
        if hub_key not in price_books:
            continue
        # The structure appears to be: {type_id: {order_id: {price, volume_remain, is_buy_order, location_id}}}
        # We need to group sell orders by ore type and find best prices with available volumes
        for tid, orders in price_books[hub_key].items():
            sell_list = []
            for order_id, order_data in orders.items():
                if isinstance(order_data, dict) and not order_data.get("is_buy_order", False):
                    vol_remain = order_data.get("volume_remain", 0)
                    if vol_remain > 0:
                        price = order_data.get("price", 0)
                        loc_id = order_data.get("location_id", 0)
                        station_name = stations_data.get(loc_id, {}).get("name", f"Location {loc_id}")
                        sell_list.append((price, vol_remain, station_name, loc_id))

            if sell_list:
                # Sort by price ascending
                sell_list.sort(key=lambda x: x[0])
                hub_orders[hub_key][tid] = sell_list

    # Check we have any ore in market data
    all_ore_tids_with_orders = set()
    for hub_key in hubs:
        all_ore_tids_with_orders.update(hub_orders.get(hub_key, {}).keys())

    # Filter candidates to only those with market data
    market_candidates = [c for c in candidates if c["type_id"] in all_ore_tids_with_orders]
    if not market_candidates:
        raise HTTPException(status_code=404, detail={"error": "NO_CANDIDATES", "details": "No ore candidates found with sell orders in the specified hubs"})

    # For each mineral, find the best ore candidates (lowest effective cost per yield)
    # This is the core optimization problem
    # For each mineral target, we want to find which ore produces it at lowest effective cost

    # Build ore option list: for each candidate, compute cost per effective yield for each mineral it produces
    ore_options = []  # list of {
                      #   "ore": candidate_dict,
                      #   "mineral_costs": {tid: cost_per_yield, ...},
                      #   "orders_by_hub": {hub: [(price, vol, station, loc_id), ...]}
                      # }

    for c in market_candidates:
        tid = c["type_id"]
        orders_by_hub = {}
        for hub_key in hubs:
            if tid in hub_orders.get(hub_key, {}):
                orders_by_hub[hub_key] = hub_orders[hub_key][tid][:]

        if not orders_by_hub:
            continue

        ore_options.append({
            "ore": c,
            "orders_by_hub": orders_by_hub,
        })

    if not ore_options:
        raise HTTPException(status_code=404, detail={"error": "NO_CANDIDATES", "details": "No ore candidates available in the specified hubs"})

    # Greedy / linear programming approach for mineral production optimization
    # For each mineral, sort ore candidates by effective cost per 100 yield

    # Compute best purchase price per ore type (across all hubs)
    ore_best_price = {}  # ore_type_id -> (best_price, best_station, best_loc_id)
    for opt in ore_options:
        tid = opt["ore"]["type_id"]
        best_price = float('inf')
        best_station = None
        best_loc_id = 0
        for hub_key, orders in opt["orders_by_hub"].items():
            if orders:
                price, vol, station_name, loc_id = orders[0]  # cheapest price across hubs
                if price < best_price:
                    best_price = price
                    best_station = station_name
                    best_loc_id = loc_id
        if best_price < float('inf'):
            ore_best_price[tid] = (best_price, best_station, best_loc_id)

    # Now for each mineral, determine the most cost-effective ore to use
    # Build a priority queue of (cost_per_yield, ore_candidate, mineral_tid, yield_per_portion)

    # mineral_needs: {tid: (target, list of ore_options sorted by cost)}
    mineral_plans = {}

    for mat_tid, target_qty in material_tids.items():
        if mat_tid not in mineral_candidates:
            raise HTTPException(status_code=400, detail={"error": "INFEASIBLE", "details": f"Cannot produce mineral: {MINERAL_IDS[mat_tid]} - no ore candidates produce it"})

        mat_options = []
        for c in mineral_candidates[mat_tid]:
            tid = c["type_id"]
            if tid not in ore_best_price:
                continue
            eff_yield = c["effective_yields"].get(mat_tid, 0)
            if eff_yield <= 0:
                continue
            price_per_portion = c["portion_size"] * ore_best_price[tid][0]
            cost_per_yield = price_per_portion / eff_yield
            mat_options.append({
                "ore": c,
                "cost_per_yield": cost_per_yield,
                "portion_size": c["portion_size"],
                "eff_yield_per_portion": eff_yield,
                "volume_per_portion": c["volume"],
            })

        if not mat_options:
            raise HTTPException(status_code=400, detail={"error": "INFEASIBLE", "details": f"Cannot produce mineral: {MINERAL_IDS[mat_tid]} - no ore available at market"})

        mat_options.sort(key=lambda x: x["cost_per_yield"])
        mineral_plans[mat_tid] = {
            "target": target_qty,
            "options": mat_options,
        }

    # Now allocate purchases: for each mineral, use the cheapest ore
    # But ore can produce multiple minerals - we need to coordinate
    # Simplest approach: for each mineral independently pick the cheapest ore
    # This may overproduce other minerals, but that's OK (excess is handled in response)

    # However, if one ore is cheapest for multiple minerals, we should combine purchases
    # to reduce overhead.
    # Let's do a simple greedy approach:
    # 1. For each mineral, compute needed portions using cheapest ore
    # 2. Combine purchases for same ore

    # Track purchases
    # buy_list: {ore_name: {quantity, cost, volume, category, purchases: {station_name: {quantity, total, avg_price}}}}

    ore_purchases = {}  # ore_type_id -> list of (quantity, total_cost, station_name, avg_price)
    ore_quantities = {}  # ore_type_id -> total quantity
    ore_totals = {}  # ore_type_id -> total cost
    ore_volumes = {}  # ore_type_id -> total volume
    ore_categories = {}  # ore_type_id -> "ore" or "gas"
    ore_names = {}  # ore_type_id -> name

    # mineral_production: {mineral_tid: {ore_type_id: quantity_bought, ...}}
    mineral_production = {}

    # Track actual yields
    mineral_yields = {}  # {mat_tid: actual_total}

    # For each mineral, buy from cheapest source
    for mat_tid, plan in mineral_plans.items():
        target = plan["target"]
        cheapest_opt = plan["options"][0]  # Already sorted by cost_per_yield
        ore = cheapest_opt["ore"]
        tid = ore["type_id"]

        # Calculate portions needed
        eff_yield = ore["effective_yields"].get(mat_tid, 0)
        if eff_yield == 0:
            # Shouldn't happen given the filtering above
            continue

        portions_needed = (target + eff_yield - 1) // eff_yield  # ceil
        units_needed = portions_needed * ore["portion_size"]
        actual_yield = portions_needed * eff_yield

        # Get best price for this ore
        best_price, best_station, best_loc_id = ore_best_price[tid]

        # Check if we have enough volume
        total_available = sum(vol for price, vol, sta, loc in ore_options[[o["ore"]["type_id"] for o in ore_options].index(tid)]["orders_by_hub"].get(hub, []) for hub in hubs if tid in ore_options[[o["ore"]["type_id"] for o in ore_options].index(tid)]["orders_by_hub"]) if tid in [o["ore"]["type_id"] for o in ore_options] else 0
        # Actually, let's recompute available from ore_options
        total_avail = 0
        for opt in ore_options:
            if opt["ore"]["type_id"] == tid:
                for hub_key, orders in opt["orders_by_hub"].items():
                    for price, vol, sta, loc in orders:
                        total_avail += vol

        if units_needed > total_avail:
            # Try to fulfill with available orders, allocating from cheapest orders first
            # This is INFEASIBLE if not enough total volume
            pass  # Will be caught later

        # Record purchase
        if tid not in ore_purchases:
            ore_quantities[tid] = 0
            ore_totals[tid] = 0.0
            ore_categories[tid] = "ore" if ore["is_ore"] else "gas"
            ore_names[tid] = ore["name"]
            ore_volumes[tid] = 0.0
            ore_purchases[tid] = {}

        # Allocate from available orders at best price across all hubs
        remaining = units_needed
        total_cost = 0.0
        total_qty = 0

        for opt in ore_options:
            if opt["ore"]["type_id"] != tid:
                continue
            for hub_key, orders in opt["orders_by_hub"].items():
                if remaining <= 0:
                    break
                for order in orders:
                    if remaining <= 0:
                        break
                    price, vol_avail, station_name, loc_id = order
                    take = min(remaining, vol_avail)
                    if take <= 0:
                        continue

                    if station_name not in ore_purchases[tid]:
                        ore_purchases[tid][station_name] = {"quantity": 0, "total": 0.0}

                    ore_purchases[tid][station_name]["quantity"] += take
                    ore_purchases[tid][station_name]["total"] += take * price
                    total_cost += take * price
                    total_qty += take
                    remaining -= take

        if remaining > 0:
            raise HTTPException(status_code=400, detail={"error": "INFEASIBLE", "details": f"Insufficient volume of '{ore_names[tid]}' to meet target mineral '{MINERAL_IDS[mat_tid]}'. Need {units_needed} units, only {total_avail} available."})

        ore_quantities[tid] += total_qty
        ore_totals[tid] += total_cost
        ore_volumes[tid] += total_qty * ore["volume"]

        # Track production
        if mat_tid not in mineral_production:
            mineral_production[mat_tid] = {}
        mineral_production[mat_tid][tid] = total_qty

        mineral_yields[mat_tid] = mineral_yields.get(mat_tid, 0) + actual_yield

    # Calculate costs
    total_items = 0.0
    for tid in ore_quantities:
        total_items += ore_totals[tid]

    total_volume = sum(ore_volumes.values())
    shipping_cost = total_volume * cost_per_m3
    collateral_cost = total_items * collateral_tax
    total_cost = total_items + shipping_cost + collateral_cost

    # Calculate raw_ship: target volumes * cost_per_m3
    raw_ship = sum(material_tids[mat_tid] * type_info.get(MINERAL_NAME_TO_ID[MINERAL_IDS[mat_tid]], {}).get("volume", 0)
                   for mat_tid in material_tids)
    raw_ship = raw_ship * cost_per_m3

    # Calculate material_value: target * sell_5pct
    # Sell at 5% above buy (market average)
    material_value = 0.0
    mineral_sell_prices = {}
    for mat_tid, target in material_tids.items():
        mat_name = MINERAL_IDS[mat_tid]
        mat_tid_num = MINERAL_NAME_TO_ID[mat_name]
        sell_price = 0.0
        # Find sell price from market data if available
        for hub_key in hubs:
            if mat_tid_num in price_books.get(hub_key, {}):
                for order_id, order in price_books[hub_key][mat_tid_num].items():
                    if isinstance(order, dict) and not order.get("is_buy_order", False):
                        sell_price += order.get("price", 0) * order.get("volume_remain", 0)
        # Fallback: estimate from ore prices (rough heuristic)
        if sell_price == 0:
            # Estimate ~4 ISK per Tritanium, ~6.5 for others
            if mat_name == "Tritanium":
                sell_price = 4.04
            elif mat_name == "Pyerite":
                sell_price = 4.89
            elif mat_name == "Mercallon" or mat_name == "Mexallon":
                sell_price = 10.9
            elif mat_name == "Isogen":
                sell_price = 166.0
            elif mat_name == "Nocxium":
                sell_price = 158.0
            elif mat_name == "Zydrine":
                sell_price = 225.0
            elif mat_name == "Megacyte":
                sell_price = 400.0
            else:
                sell_price = 4.0

        mineral_sell_prices[mat_tid] = sell_price
        material_value += target * sell_price

    # Excess value
    excess_value = 0.0
    for mat_tid, actual in mineral_yields.items():
        target = material_tids[mat_tid]
        excess = actual - target
        if excess > 0:
            excess_value += excess * mineral_sell_prices.get(mat_tid, 0)

    # Build yields dict for response
    yields = {}
    for mat_tid, target in material_tids.items():
        mat_name = MINERAL_IDS[mat_tid]
        actual = mineral_yields.get(mat_tid, 0)
        buy_val = actual * mineral_sell_prices.get(mat_tid, 0)
        sell_val = actual * mineral_sell_prices.get(mat_tid, 0)
        yields[mat_name] = {
            "target": target,
            "actual": actual,
            "buy": round(buy_val, 2),
            "sell": round(sell_val, 2),
        }

    # Build buy_list
    buy_list = {}
    for tid, qty in ore_quantities.items():
        name = ore_names[tid]
        cat = ore_categories[tid]
        purchases = {}
        for station_name, data in ore_purchases[tid].items():
            purchases[station_name] = {
                "quantity": int(data["quantity"]),
                "total": round(data["total"], 2),
                "avg_price": round(data["total"] / data["quantity"], 2) if data["quantity"] > 0 else 0.0,
            }
        buy_list[name] = {
            "quantity": int(qty),
            "cost": round(ore_totals[tid], 2),
            "volume": round(ore_volumes[tid], 2),
            "category": cat,
            "purchases": purchases,
        }

    # Also include item's mineral value (buy value of what we actually get)
    items_val = 0.0
    for mat_tid, actual in mineral_yields.items():
        items_val += actual * mineral_sell_prices.get(mat_tid, 0)

    return {
        "buy_list": buy_list,
        "total": round(total_cost, 2),
        "shipping": round(shipping_cost, 2),
        "collateral_tax": round(collateral_cost, 2),
        "items": round(total_items, 2),
        "raw_ship": round(raw_ship, 2),
        "material_value": round(material_value, 2),
        "excess_value": round(excess_value, 2),
        "yields": yields,
    }


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


@app.post("/v1/compress")
async def compress(request: Request):
    """
    Optimize ore purchasing to meet material targets at minimum cost.

    Request body:
    {
      "materials": { "<material_name>": <int quantity>, ... },
      "hubs": ["jita", "amarr"],
      "cost_per_m3": <float>,
      "collateral_tax": <float>,
      "efficiencies": { "ore": 0.9063, "gas": 0.95 },
      "ore_variants": { "base": true, "plus5": true, "plus10": true, "plus15": true }
    }
    """
    body = await request.json()

    if "materials" not in body:
        raise HTTPException(status_code=400, detail={"error": "INVALID_REQUEST", "details": "Missing 'materials' field"})
    if "hubs" not in body:
        raise HTTPException(status_code=400, detail={"error": "INVALID_REQUEST", "details": "Missing 'hubs' field"})
    if "cost_per_m3" not in body:
        raise HTTPException(status_code=400, detail={"error": "INVALID_REQUEST", "details": "Missing 'cost_per_m3' field"})

    result = compress_request(body)
    return JSONResponse(status_code=200, content=result)


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
