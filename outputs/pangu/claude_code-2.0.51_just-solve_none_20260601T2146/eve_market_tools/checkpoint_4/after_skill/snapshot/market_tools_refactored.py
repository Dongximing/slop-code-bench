#!/usr/bin/env python3
"""Market Tools API - Industrialist market analysis tools."""

import gzip
import csv
import json
import secrets
from collections import defaultdict
from typing import Any

from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import JSONResponse
import uvicorn

app = FastAPI()

price_books: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
stations_data: dict[int, dict[str, Any]] = {}

# Volume history: {(region_id, type_id): {date: {average, highest, lowest, count, volume}}}
volumes_history: dict[tuple[int, int], dict[str, dict[str, Any]]] = {}

type_names: dict[int, str] = {}
type_names_lower: dict[str, int] = {}
type_info: dict[int, dict[str, Any]] = {}
type_materials: dict[int, list[tuple[int, int]]] = {}
groups: dict[int, dict[str, Any]] = {}
categories: dict[int, str] = {}
group_categories: dict[int, int] = {}
api_keys: dict[str, dict[str, Any]] = {}

# Effective timestamp for competition analysis
effective_timestamp: float = 0.0

# Track station IDs for competition analysis
station_systems: dict[int, int] = {}  # station_id -> solar_system_id
sde_loaded = False

STRUCTURE_MODIFIERS = {"npc_station": 0.00, "citadel": 0.00, "athanor": 0.02, "tatara": 0.055}
RIG_MODIFIERS = {"null": 0, "t1": 1, "t2": 3}
SECURITY_MODIFIERS = {"highsec": 0.00, "lowsec": 0.06, "nullsec": 0.12}
IMPLANT_MODIFIERS = {"null": 0.00, "RX-801": 0.01, "RX-802": 0.02, "RX-804": 0.04}
MAIN_HUBS = {"jita": 60003760, "amarr": 60008494, "dodixie": 60011866, "rens": 60004588, "hek": 60005686}
HUB_NAMES = {"jita": "Jita", "amarr": "Amarr", "dodixie": "Dodixie", "rens": "Rens", "hek": "Hek"}
DEFAULT_EFFICIENCIES = {"ore": 0.9063, "gas": 0.95, "scrap": 0.55}
MINERAL_IDS = {34: "Tritanium", 35: "Pyerite", 36: "Mexallon", 37: "Isogen", 38: "Nocxium", 39: "Zydrine", 40: "Megacyte"}
MINERAL_NAME_TO_ID = {v: k for k, v in MINERAL_IDS.items()}
ORE_GROUPS = {450, 451, 452, 453, 454, 455, 456, 457, 458, 459, 460, 461, 462, 467, 468, 469}

VARIANT_MAP = {
    "": 1.0, "Condensed": 1.05, "Massive": 1.10, "Glossy": 1.15, "Crimson": 1.03,
    "Prime": 1.06, "Triclinic": 1.03, "Monoclinic": 1.06, "Sharp": 1.05, "Crystalline": 1.08,
    "Obsidian": 1.04, "Onyx": 1.03, "Vitric": 1.03, "Glazed": 1.06, "Vivid": 1.04,
    "Radiant": 1.08, "Pure": 1.03, "Pristine": 1.06, "Fiery": 1.03, "Luminous": 1.06,
    "Azure": 1.03, "Rich": 1.06, "Solid": 1.03, "Viscous": 1.06, "Bright": 1.04,
    "Gleaming": 1.08, "Iridescent": 1.05, "Prismatic": 1.10, "Silvery": 1.03,
    "Golden": 1.06, "Magma": 1.03, "Vitreous": 1.06,
}
VARIANT_TIERS = {
    frozenset({"": "base"}): "base",
    frozenset({"Condensed", "Massive", "Glossy"}): "plus10",
    frozenset({"Crimson", "Prime", "Triclinic", "Monoclinic", "Sharp", "Crystalline",
               "Obsidian", "Onyx", "Vitric", "Glazed", "Vivid", "Radiant", "Pure",
               "Pristine", "Fiery", "Luminous", "Azure", "Solid", "Viscous", "Bright",
               "Silvery", "Magma"}): "base",
    frozenset({"Gleaming", "Iridescent", "Rich"}): "plus5",
    frozenset({"Prismatic", "Golden"}): "plus10",
}


def open_any(filepath: str):
    for opener in (lambda p: gzip.open(p, 'rt', encoding='utf-8'),
                    lambda p: open(p, 'r', encoding='utf-8')):
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
            type_info[tid] = {"group_id": int(data["group_id"]), "volume": float(data["volume"]), "portion_size": int(data["portion_size"])}
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
                    type_info[tid] = {"group_id": int(row['groupID']), "volume": float(row['volume']), "portion_size": int(row['portionSize'])}
                except (ValueError, KeyError):
                    continue

    with open_any(f"{sde_path}/invTypeMaterials.csv.bz2") as f:
        if f:
            for row in csv.DictReader(f):
                try:
                    type_materials.setdefault(int(row['typeID']), []).append((int(row['materialTypeID']), int(row['quantity'])))
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
                    categories[int(row['categoryID'])] = row['categoryName']
                except (ValueError, KeyError):
                    continue

    sde_loaded = True
    print(f"SDE loaded: {len(type_names_lower)} types, {len(type_materials)} materials")

    # Load station to system mapping for competition analysis
    try:
        with open_any(f"{sde_path}/staStations.csv.bz2") as f:
            if f:
                for row in csv.DictReader(f):
                    try:
                        station_id = int(row['stationID'])
                        system_id = int(row['solarSystemID'])
                        station_systems[station_id] = system_id
                    except (ValueError, KeyError):
                        pass
        print(f"Loaded {len(station_systems)} station mappings")
    except Exception as e:
        print(f"Could not load station mappings: {e}")


def _detect_variant(name: str) -> tuple[str | None, float]:
    for variant_name, multiplier in VARIANT_MAP.items():
        if variant_name and variant_name in name:
            return variant_name, multiplier
    return None, 1.0


def _get_variant_tier(variant_name: str | None) -> str:
    if variant_name is None:
        return "base"
    for variants, tier in VARIANT_TIERS.items():
        if variant_name in variants:
            return tier
    return "base"


def _get_ore_candidates(efficiencies: dict, ore_variants: dict) -> list[dict]:
    candidates = []
    for tid, name in type_names.items():
        info = type_info.get(tid)
        if not info:
            continue
        gid = info.get("group_id", 0)
        volume = info.get("volume", 0)
        portion_size = info.get("portion_size", 1)
        materials = type_materials.get(tid, [])
        if not materials or portion_size <= 0 or volume <= 0:
            continue
        is_ore = gid in ORE_GROUPS
        is_gas = gid in {422, 1033}
        if not (is_ore or is_gas):
            continue

        variant_name, variant_mult = _detect_variant(name)
        tier = _get_variant_tier(variant_name)
        if variant_name is None:
            if not ore_variants.get("base", False):
                continue
        elif variant_name in {"Condensed", "Massive", "Glossy"}:
            if not ore_variants.get(f"plus{int((variant_mult - 1) * 100)}", False):
                continue
        elif tier == "base":
            if not ore_variants.get("base", False):
                continue
        elif tier == "plus5":
            if not (ore_variants.get("base", False) or ore_variants.get("plus5", False)):
                continue
        elif tier == "plus10":
            if not (ore_variants.get("base", False) or ore_variants.get("plus5", False) or ore_variants.get("plus10", False)):
                continue

        cat = "ore" if is_ore else "gas"
        eff = efficiencies.get(cat, DEFAULT_EFFICIENCIES.get(cat, 0.5))
        effective_yields = {mat_tid: int(mat_qty * variant_mult * eff) for mat_tid, mat_qty in materials}

        candidates.append({
            "type_id": tid, "name": name, "volume": volume, "portion_size": portion_size,
            "materials": materials, "effective_yields": effective_yields,
            "is_ore": is_ore, "is_gas": is_gas, "variant_mult": variant_mult,
        })
    return candidates


def _build_hub_orders(hubs: list[str]) -> dict:
    hub_orders = {}
    for hub_key in hubs:
        hub_orders[hub_key] = {}
        if hub_key not in price_books:
            continue
        for tid, orders in price_books[hub_key].items():
            sell_list = []
            for order_id, order_data in orders.items():
                if isinstance(order_data, dict) and not order_data.get("is_buy_order", False):
                    vol_remain = order_data.get("volume_remain", 0)
                    if vol_remain > 0:
                        sell_list.append((order_data.get("price", 0), vol_remain,
                                         stations_data.get(order_data.get("location_id", 0), {}).get("name", f"Location {order_data.get('location_id', 0)}"),
                                         order_data.get("location_id", 0)))
            if sell_list:
                sell_list.sort(key=lambda x: x[0])
                hub_orders[hub_key][tid] = sell_list
    return hub_orders


def _compute_ore_best_price(ore_options: list, hubs: list[str]) -> dict:
    ore_best_price = {}
    for opt in ore_options:
        tid = opt["ore"]["type_id"]
        best_price = float('inf')
        best_station = best_loc_id = None
        for hub_key in hubs:
            orders = opt["orders_by_hub"].get(hub_key, [])
            if orders and orders[0][0] < best_price:
                best_price, _, best_station, best_loc_id = orders[0]
        if best_price < float('inf'):
            ore_best_price[tid] = (best_price, best_station, best_loc_id)
    return ore_best_price


def compress_request(req_body: dict) -> dict:
    materials_target = req_body.get("materials", {})
    hubs = [h.lower() for h in req_body.get("hubs", [])]
    cost_per_m3 = req_body.get("cost_per_m3", 350.0)
    collateral_tax = req_body.get("collateral_tax", 0.0)
    efficiencies = req_body.get("efficiencies", DEFAULT_EFFICIENCIES.copy())
    ore_variants = req_body.get("ore_variants", {"base": True, "plus5": True, "plus10": True, "plus15": True})

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

    valid_hubs = set(MAIN_HUBS.keys())
    for hub in hubs:
        if hub not in valid_hubs:
            raise HTTPException(status_code=400, detail={"error": "INVALID_REQUEST", "details": f"Invalid hub: {hub}. Valid hubs: {list(valid_hubs)}"})

    candidates = _get_ore_candidates(efficiencies, ore_variants)
    if not candidates:
        raise HTTPException(status_code=404, detail={"error": "NO_CANDIDATES", "details": "No ore candidates found matching the specified filters"})

    mineral_candidates = {}
    for c in candidates:
        for mat_tid, eff_yield in c["effective_yields"].items():
            if mat_tid in MINERAL_IDS:
                mineral_candidates.setdefault(mat_tid, []).append(c)

    hub_orders = _build_hub_orders(hubs)
    all_ore_tids_with_orders = {tid for hub_o in hub_orders.values() for tid in hub_o}
    market_candidates = [c for c in candidates if c["type_id"] in all_ore_tids_with_orders]
    if not market_candidates:
        raise HTTPException(status_code=404, detail={"error": "NO_CANDIDATES", "details": "No ore candidates found with sell orders in specified hubs"})

    ore_options = []
    for c in market_candidates:
        tid = c["type_id"]
        orders_by_hub = {hub_key: hub_orders.get(hub_key, {}).get(tid, []) for hub_key in hubs if hub_key in hub_orders and tid in hub_orders.get(hub_key, {})}
        if orders_by_hub:
            ore_options.append({"ore": c, "orders_by_hub": orders_by_hub})
    if not ore_options:
        raise HTTPException(status_code=404, detail={"error": "NO_CANDIDATES", "details": "No ore candidates available in specified hubs"})

    ore_best_price = _compute_ore_best_price(ore_options, hubs)

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
            mat_options.append({"ore": c, "cost_per_yield": price_per_portion / eff_yield, "portion_size": c["portion_size"], "eff_yield_per_portion": eff_yield, "volume_per_portion": c["volume"]})
        if not mat_options:
            raise HTTPException(status_code=400, detail={"error": "INFEASIBLE", "details": f"Cannot produce mineral: {MINERAL_IDS[mat_tid]} - no ore available at market"})
        mat_options.sort(key=lambda x: x["cost_per_yield"])

    ore_purchases = {}
    ore_quantities = {}
    ore_totals = {}
    ore_volumes = {}
    ore_categories = {}
    ore_names = {}
    mineral_production = {}
    mineral_yields = {}

    for mat_tid, plan in material_tids.items():
        cheapest_opt = min(
            ([o for o in mineral_candidates[mat_tid] if o["type_id"] in ore_best_price],
             key=lambda c: (c["portion_size"] * ore_best_price[c["type_id"]][0]) / c["effective_yields"].get(mat_tid, 1))[0]
            if mineral_candidates[mat_tid] else None
        )
        tid = cheapest_opt["type_id"]
        ore = cheapest_opt
        eff_yield = ore["effective_yields"].get(mat_tid, 0)
        portions_needed = (material_tids[mat_tid] + eff_yield - 1) // eff_yield
        units_needed = portions_needed * ore["portion_size"]
        actual_yield = portions_needed * eff_yield
        best_price, best_station, best_loc_id = ore_best_price[tid]

        total_avail = sum(vol for opt in ore_options if opt["ore"]["type_id"] == tid
                         for orders in opt["orders_by_hub"].values()
                         for _, vol, _, _ in orders)
        if units_needed > total_avail:
            raise HTTPException(status_code=400, detail={"error": "INFEASIBLE", "details": f"Insufficient volume of '{ore['name']}' to meet target mineral '{MINERAL_IDS[mat_tid]}'. Need {units_needed} units, only {total_avail} available."})

        if tid not in ore_purchases:
            ore_quantities[tid] = 0
            ore_totals[tid] = 0.0
            ore_categories[tid] = "ore" if ore["is_ore"] else "gas"
            ore_names[tid] = ore["name"]
            ore_volumes[tid] = 0.0
            ore_purchases[tid] = {}

        remaining = units_needed
        total_cost = 0.0
        total_qty = 0

        for opt in ore_options:
            if opt["ore"]["type_id"] != tid or remaining <= 0:
                continue
            for hub_key, orders in opt["orders_by_hub"].items():
                if remaining <= 0:
                    break
                for price, vol_avail, station_name, loc_id in orders:
                    if remaining <= 0:
                        break
                    take = min(remaining, vol_avail)
                    if take <= 0:
                        continue
                    ore_purchases[tid].setdefault(station_name, {"quantity": 0, "total": 0.0})
                    ore_purchases[tid][station_name]["quantity"] += take
                    ore_purchases[tid][station_name]["total"] += take * price
                    total_cost += take * price
                    total_qty += take
                    remaining -= take

        ore_quantities[tid] += total_qty
        ore_totals[tid] += total_cost
        ore_volumes[tid] += total_qty * ore["volume"]
        mineral_production.setdefault(mat_tid, {})[tid] = total_qty
        mineral_yields[mat_tid] = mineral_yields.get(mat_tid, 0) + actual_yield

    total_items = sum(ore_totals.values())
    total_volume = sum(ore_volumes.values())
    shipping_cost = total_volume * cost_per_m3
    collateral_cost = total_items * collateral_tax
    total_cost = total_items + shipping_cost + collateral_cost

    raw_ship = sum(material_tids[mat_tid] * type_info.get(next((k for k, v in MINERAL_IDS.items() if v == MINERAL_IDS[mat_tid]), 0), {}).get("volume", 0)
                   for mat_tid in material_tids) * cost_per_m3

    mineral_sell_prices = {}
    material_value = 0.0
    for mat_tid, target in material_tids.items():
        mat_name = MINERAL_IDS[mat_tid]
        sell_price = 0.0
        for hub_key in hubs:
            if mat_tid in price_books.get(hub_key, {}):
                for order_id, order in price_books[hub_key][mat_tid].items():
                    if isinstance(order, dict) and not order.get("is_buy_order", False):
                        sell_price += order.get("price", 0) * order.get("volume_remain", 0)
        if sell_price == 0:
            sell_price = {"Tritanium": 4.04, "Pyerite": 4.89, "Mexallon": 10.9, "Isogen": 166.0, "Nocxium": 158.0, "Zydrine": 225.0, "Megacyte": 400.0}.get(mat_name, 4.0)
        mineral_sell_prices[mat_tid] = sell_price
        material_value += target * sell_price

    excess_value = sum((mineral_yields.get(mat_tid, 0) - target) * mineral_sell_prices.get(mat_tid, 0)
                       for mat_tid, target in material_tids.items() if mineral_yields.get(mat_tid, 0) > target)

    yields = {MINERAL_IDS[mat_tid]: {"target": target, "actual": mineral_yields.get(mat_tid, 0), "buy": round(mineral_yields.get(mat_tid, 0) * mineral_sell_prices.get(mat_tid, 0), 2), "sell": round(mineral_yields.get(mat_tid, 0) * mineral_sell_prices.get(mat_tid, 0), 2)}
              for mat_tid, target in material_tids.items()}

    buy_list = {}
    for tid, qty in ore_quantities.items():
        name = ore_names[tid]
        cat = ore_categories[tid]
        purchases = {station: {"quantity": int(data["quantity"]), "total": round(data["total"], 2), "avg_price": round(data["total"] / data["quantity"], 2) if data["quantity"] > 0 else 0.0} for station, data in ore_purchases[tid].items()}
        buy_list[name] = {"quantity": int(qty), "cost": round(ore_totals[tid], 2), "volume": round(ore_volumes[tid], 2), "category": cat, "purchases": purchases}

    items_val = sum(mineral_yields.get(mat_tid, 0) * mineral_sell_prices.get(mat_tid, 0) for mat_tid in material_tids)

    return {"buy_list": buy_list, "total": round(total_cost, 2), "shipping": round(shipping_cost, 2), "collateral_tax": round(collateral_cost, 2), "items": round(total_items, 2), "raw_ship": round(raw_ship, 2), "material_value": round(material_value, 2), "excess_value": round(excess_value, 2), "yields": yields}


def get_type_id(name: str) -> int | None:
    return type_names_lower.get(name.lower())


def get_type_name(type_id: int) -> str:
    return type_names.get(type_id, f"Type {type_id}")


def is_valid_type_id(type_id: int) -> bool:
    return type_id in type_info


def get_item_category(item_tid: int) -> str:
    info = type_info.get(item_tid)
    if not info:
        return "unknown"
    group_id = info.get("group_id", 0)
    cat_id = group_categories.get(group_id, 0)
    if cat_id == 25:
        return "ore"
    if group_id in {465, 423, 903}:
        return "ice"
    if group_id in {422, 1033}:
        return "gas"
    return "scrap"


def calculate_yield(structure_config: dict, skills: dict, implant: str) -> dict[str, float]:
    sm = STRUCTURE_MODIFIERS.get(structure_config.get("type", "npc_station"), 0.00)
    rm = RIG_MODIFIERS.get(structure_config.get("rig", "null"), 0)
    sec = SECURITY_MODIFIERS.get(structure_config.get("security", "highsec"), 0.00)
    im_ = IMPLANT_MODIFIERS.get(implant, 0.00)
    r = skills.get("Reprocessing", 0)
    re = skills.get("Reprocessing Efficiency", 0)

    base = 50 + rm
    mult = base * (1 + sec) * (1 + sm) ** 2 * (1 + r * 0.03) * (1 + re * 0.02)

    op_gas = skills.get("Gas Cloud Harvesting", 0)
    return {"ore": round(mult * (1 + skills.get("Ore Processing", 0) * 0.02) * (1 + im_), 2), "gas": round(mult * (1 + op_gas * 0.02) * (1 + im_), 2), "scrap": float(min(55, 50 + skills.get("Scrapfield Processing", re)))}


def get_efficiency_for_item(item_tid: int, efficiencies: dict) -> float:
    return efficiencies.get(get_item_category(item_tid), DEFAULT_EFFICIENCIES.get(get_item_category(item_tid), 0.5))


def reprocess_items(items: dict, efficiencies: dict, drill_down: bool = False, market: str = None) -> dict:
    item_tids = {}
    input_volume = 0.0
    for name, qty in items.items():
        tid = get_type_id(name)
        if not tid or tid not in type_info:
            raise HTTPException(status_code=404, detail={"error": "UNKNOWN_ITEMS", "details": sorted([name])})
        if not isinstance(qty, int) or qty <= 0:
            raise HTTPException(status_code=400, detail={"error": "INVALID_REQUEST", "details": f"Quantity must be positive integer for item: {name}"})
        item_tids[name] = tid
        input_volume += qty * float(type_info[tid].get("volume", 0))

    all_products = {}
    total_materials = []
    reprocessable_outputs = {}

    for name, tid in item_tids.items():
        qty = items[name]
        portion_size = type_info[tid].get("portion_size", 1)
        num_portions = qty // portion_size
        leftover = qty % portion_size

        if leftover:
            leftover_vol = leftover * float(type_info[tid].get("volume", 0))
            all_products[name] = (all_products.get(name, (0, 0.0))[0] + leftover, all_products.get(name, (0, 0.0))[1] + leftover_vol)

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
        all_products[mat_name] = (all_products.get(mat_name, (0, 0.0))[0] + mat_qty, all_products.get(mat_name, (0, 0.0))[1] + mat_vol)

    if drill_down and reprocessable_outputs:
        drill_outputs = {mat_name: qty for mat_name, (qty, _) in all_products.items() if get_type_id(mat_name) and get_type_id(mat_name) in type_materials}
        if drill_outputs:
            drill_result = reprocess_items(drill_outputs, efficiencies, drill_down=False, market=None)
            for prod_name, prod_data in drill_result["products"].items():
                all_products[prod_name] = (all_products.get(prod_name, (0, 0.0))[0] + prod_data["quantity"], all_products.get(prod_name, (0, 0.0))[1] + prod_data["volume"])

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
                type_orders = [o for orders in price_books[market].values() for o in [orders] if isinstance(o, dict) and o.get("type_id") == tid]
                if type_orders:
                    buy_orders = [o for o in type_orders if o.get("is_buy_order")]
                    sell_orders = [o for o in type_orders if not o.get("is_buy_order")]
                    if sell_orders:
                        products[name]["sell"] = round(min(o["price"] for o in sell_orders), 2)
                    if buy_orders:
                        products[name]["buy"] = round(max(o["price"] for o in buy_orders), 2)

    return {"inputs": {"volume": round(input_volume, 2), "buy": None, "sell": None, "buy_5pct": None, "sell_5pct": None}, "outputs": {"volume": round(output_volume, 2), "buy": None, "sell": None, "buy_5pct": None, "sell_5pct": None}, "yields": efficiencies.copy(), "products": products}


def verify_api_key(key: str) -> dict | None:
    return api_keys.get(key)


def generate_api_key(config: dict) -> str:
    key = secrets.token_urlsafe(32)
    api_keys[key] = config
    return key


def _validate_structure(structure: dict) -> dict | None:
    if not structure or "type" not in structure:
        return None
    if structure["type"] not in STRUCTURE_MODIFIERS:
        return JSONResponse(status_code=404, content={"code": "INVALID_VALUE", "path": "structure.type", "message": f"Invalid structure type: {structure['type']}"})
    if structure["type"] == "npc_station" and structure.get("rig") is not None:
        return JSONResponse(status_code=404, content={"code": "INVALID_VALUE", "path": "structure.rig", "message": "npc_station cannot specify a rig"})
    if "rig" in structure and structure["rig"] not in RIG_MODIFIERS:
        return JSONResponse(status_code=404, content={"code": "INVALID_VALUE", "path": "structure.rig", "message": f"Invalid rig type: {structure['rig']}"})
    if "security" in structure and structure["security"] not in SECURITY_MODIFIERS:
        return JSONResponse(status_code=404, content={"code": "INVALID_VALUE", "path": "structure.security", "message": f"Invalid security status: {structure['security']}"})
    return None


def _validate_skills(skills: dict) -> dict | None:
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
    config = {"structure": {"type": body.get("structure", {}).get("type", "npc_station"), "rig": body.get("structure", {}).get("rig"), "security": body.get("structure", {}).get("security", "highsec")}, "skills": body.get("skills", {}), "implant": body.get("implant")}
    return JSONResponse(status_code=201, content={"key": generate_api_key(config)})


@app.post("/v1/reprocess")
async def reprocess_endpoint(request: Request, x_api_key: str | None = Header(None, alias="X-API-Key")):
    body = await request.json()
    if "items" not in body:
        raise HTTPException(status_code=400, detail={"error": "INVALID_REQUEST", "details": "Missing 'items' field"})

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

    result = reprocess_items(body["items"], yields, drill_down, market)
    return JSONResponse(status_code=201, content=result)


@app.post("/v1/compress")
async def compress_endpoint(request: Request):
    body = await request.json()
    for field in ["materials", "hubs", "cost_per_m3"]:
        if field not in body:
            raise HTTPException(status_code=400, detail={"error": "INVALID_REQUEST", "details": f"Missing '{field}' field"})
    result = compress_request(body)
    return JSONResponse(status_code=200, content=result)


@app.post("/v1/volumes")
async def volumes_endpoint(request: Request):
    """
    Upload volume history data.
    Expects CSV with columns: date, region_id, type_id, average, highest, lowest, count, volume
    """
    global volumes_history

    content_type = request.headers.get('content-type', '')
    if 'multipart/form-data' in content_type:
        form = await request.form()
        file = form.get('file')
        if file:
            csv_data = await file.read()
        else:
            raise HTTPException(status_code=400, detail={"error": "INVALID_REQUEST", "details": "Missing file in multipart upload"})
    else:
        csv_data = await request.body()

    # Parse CSV data
    try:
        if csv_data.startswith(b'\x1f\x8b'):  # gzip magic number
            import io
            with gzip.GzipFile(fileobj=io.BytesIO(csv_data)) as f:
                text = f.read().decode('utf-8')
        else:
            text = csv_data.decode('utf-8')
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": "INVALID_REQUEST", "details": f"Failed to parse uploaded data: {str(e)}"})

    # Parse CSV
    import io as io_module
    reader = csv.DictReader(io_module.StringIO(text))
    rows = list(reader)

    # Process data
    new_data = {}
    for row in rows:
        try:
            date = row['date']
            region_id = int(row['region_id'])
            type_id = int(row['type_id'])
            key = (region_id, type_id)

            data = {
                'average': float(row['average']),
                'highest': float(row['highest']),
                'lowest': float(row['lowest']),
                'count': int(row['count']),
                'volume': int(row['volume'])
            }

            if key not in new_data:
                new_data[key] = {}
            new_data[key][date] = data
        except (KeyError, ValueError) as e:
            raise HTTPException(status_code=400, detail={"error": "INVALID_REQUEST", "details": f"Invalid row data: {str(e)}"})

    # Merge with existing data
    for key, data in new_data.items():
        if key not in volumes_history:
            volumes_history[key] = {}
        volumes_history[key].update(data)

    return JSONResponse(status_code=200, content={"status": "VOLUME_UPDATED", "count": len(rows)})


@app.post("/v1/profit/reprocess")
async def profit_reprocess_endpoint(request: Request, x_api_key: str | None = Header(None, alias="X-API-Key")):
    """
    Find profitable items to buy, reprocess, and sell to buy orders.
    """
    body = await request.json()

    k = body.get('k', 10)
    hub = body.get('hub', 'jita').lower()
    sort_by = body.get('sort_by', 'roi')
    sales_tax = body.get('sales_tax', 0.04)  # Default 4%
    scrap_efficiency = body.get('scrap_efficiency', 0.55)

    # Validate hub
    valid_hubs = set(MAIN_HUBS.keys())
    if hub not in valid_hubs:
        raise HTTPException(status_code=400, detail={"error": "INVALID_REQUEST", "details": f"Invalid hub: {hub}. Valid hubs: {list(valid_hubs)}"})

    # Get API key config if present
    config = None
    if x_api_key and x_api_key in api_keys:
        config = api_keys[x_api_key]
        if 'structure' in config and 'type' in config['structure']:
            # Override sales_tax from config if present
            if 'sales_tax' in body:
                sales_tax = body['sales_tax']

    # Get scrap items (non-ore, non-gas items with materials)
    scrap_candidates = []
    for tid, name in type_names.items():
        info = type_info.get(tid)
        if not info:
            continue
        gid = info.get('group_id', 0)
        volume = info.get('volume', 0)
        portion_size = info.get('portion_size', 1)
        materials = type_materials.get(tid, [])

        # Skip items without materials or invalid volume/portion
        if not materials or portion_size <= 0 or volume <= 0:
            continue

        # Skip ore and gas
        is_ore = gid in ORE_GROUPS
        is_gas = gid in {422, 1033}
        if is_ore or is_gas:
            continue

        # This is a scrap candidate
        scrap_candidates.append({
            'type_id': tid,
            'name': name,
            'volume': volume,
            'portion_size': portion_size,
            'materials': materials
        })

    if not scrap_candidates:
        return JSONResponse(status_code=200, content={
            'cost': 0.0,
            'revenue': 0.0,
            'profit': 0.0,
            'items': []
        })

    # Find best buy orders for each scrap item (lowest price with sufficient volume)
    hub_station_id = MAIN_HUBS[hub]
    results = []

    for item in scrap_candidates:
        tid = item['type_id']
        name = item['name']

        # Get best sell orders in the hub
        best_price = float('inf')
        total_volume = 0

        if tid in price_books.get(hub, {}):
            for order_id, order in price_books[hub][tid].items():
                if isinstance(order, dict) and not order.get('is_buy_order', False):
                    price = order.get('price', 0)
                    vol = order.get('volume_remain', 0)
                    if price < best_price:
                        best_price = price
                        total_volume = vol
                    elif price == best_price:
                        total_volume += vol

        if best_price == float('inf'):
            continue

        # Calculate reprocessing yields
        eff = get_efficiency_for_item(tid, {'scrap': scrap_efficiency})
        total_material_value = 0.0

        for mat_tid, mat_qty in item['materials']:
            # Effective yield after reprocessing efficiency
            eff_yield = int(mat_qty * eff)
            mat_name = MINERAL_IDS.get(mat_tid)

            # Get best buy order price for the mineral
            mineral_buy_price = 0
            for mineral_hub in price_books.values():
                if mat_tid in mineral_hub:
                    for oid, o in mineral_hub[mat_tid].items():
                        if isinstance(o, dict) and o.get('is_buy_order', False):
                            buy_price = o.get('price', 0)
                            if buy_price > mineral_buy_price:
                                mineral_buy_price = buy_price

            total_material_value += eff_yield * mineral_buy_price

        # Calculate costs with sales tax on buying
        cost_with_tax = best_price * (1 + sales_tax)

        # Calculate profit with sales tax on both buying and selling
        profit = total_material_value - cost_with_tax

        # ROI
        roi = (profit / cost_with_tax) if cost_with_tax > 0 else 0

        # Determine quantity (we can buy up to total_volume)
        quantity = min(1000, total_volume)  # Limit to reasonable amount

        results.append({
            'type_id': tid,
            'item': name,
            'quantity': quantity,
            'cost': quantity * best_price,
            'value': total_material_value * (quantity / 1000) if quantity > 0 else 0,
            'profit': profit * (quantity / 1000) if quantity > 0 else 0,
            'roi': roi,
            'max_price': best_price,
            'total_material_value': total_material_value
        })

    # Sort by sort_by parameter
    if sort_by == 'roi':
        results.sort(key=lambda x: (-x['roi'], x['item']))  # Descending ROI, then alphabetically
    else:  # profit
        results.sort(key=lambda x: (-x['profit'], x['item']))  # Descending profit, then alphabetically

    # Limit to k items
    results = results[:k]

    # Calculate totals
    total_cost = sum(r['cost'] for r in results)
    total_revenue = sum(r['value'] for r in results)
    total_profit = sum(r['profit'] for r in results)

    # Format output
    items_output = [{
        'item': r['item'],
        'quantity': r['quantity'],
        'cost': round(r['cost'], 2),
        'value': round(r['value'], 2),
        'profit': round(r['profit'], 2),
        'roi': round(r['roi'], 4),
        'max_price': r['max_price']
    } for r in results]

    return JSONResponse(status_code=200, content={
        'cost': round(total_cost, 2),
        'revenue': round(total_revenue, 2),
        'profit': round(total_profit, 2),
        'items': items_output
    })


@app.get("/v1/profit/haul")
async def profit_haul_endpoint(
    hub: str = 'jita',
    capacity: float = 1000000.0,
    shipping: float = 350.0,
    max_jumps: int = 5,
    margin: float = 20.0,
    include_hubs: bool = False,
    daily_value: float = 1e9,
    optimize: str = 'm3'
):
    """
    Find items to buy from sell orders and haul back to the main hub.
    """
    global effective_timestamp

    hub = hub.lower()

    # Validate hub
    valid_hubs = set(MAIN_HUBS.keys())
    if hub not in valid_hubs:
        raise HTTPException(status_code=400, detail={"error": "INVALID_REQUEST", "details": f"Invalid hub: {hub}. Valid hubs: {list(valid_hubs)}"})

    # Default sales tax from API key or 4%
    sales_tax = 0.04
    # Get effective timestamp from price books
    update_effective_timestamp()

    hub_station_id = MAIN_HUBS[hub]

    # Find all items with sell orders in the hub
    items_to_consider = []

    for tid, orders in price_books.get(hub, {}).items():
        name = type_names.get(tid, f'Type {tid}')

        # Get sell orders at this station
        sell_orders = [o for o in orders.values() if isinstance(o, dict) and not o.get('is_buy_order', False) and o.get('location_id') == hub_station_id]

        if not sell_orders:
            continue

        # Get best sell price
        best_sell = min(sell_orders, key=lambda x: x.get('price', float('inf')))
        sell_price = best_sell.get('price', 0)

        # Check competition for this item
        competition = calculate_sell_competition_hub(hub, tid, 1440)  # 1 day

        # Get average daily stats
        # Use the Forge region (10000002) for The Forge, other regions for other hubs
        region_map = {
            'jita': 10000002,
            'amarr': 10000003,
            'dodixie': 10000004,
            'rens': 10000031,
            'hek': 10000032
        }
        region_id = region_map.get(hub, 10000002)
        avg_daily_value, avg_daily_volume = get_average_daily_stats(region_id, tid)

        # Filter by daily value
        if avg_daily_value < daily_value:
            continue

        items_to_consider.append({
            'type_id': tid,
            'name': name,
            'sell_price': sell_price,
            'competition': competition,
            'avg_daily_value': avg_daily_value,
            'avg_daily_volume': avg_daily_volume
        })

    # Find items to buy from other stations
    purchases = {}
    sales = {}
    total_cost = 0.0
    total_revenue = 0.0
    total_volume = 0.0

    for item in items_to_consider:
        tid = item['type_id']
        name = item['name']
        sell_price = item['sell_price']

        # Find the best buy price from other stations (lower than our sell price)
        best_buy_price = float('inf')
        best_buy_station = None
        best_buy_volume = 0

        # Search in all price books
        for hub_name, price_book in price_books.items():
            if hub_name == hub:
                continue
            if not include_hubs:
                continue
            if tid in price_book:
                for order_id, order in price_book[tid].items():
                    if isinstance(order, dict) and not order.get('is_buy_order', False):
                        price = order.get('price', 0)
                        vol = order.get('volume_remain', 0)
                        station_id = order.get('location_id', 0)
                        station_name = stations_data.get(station_id, {}).get('name', f'Station {station_id}')

                        if price < best_buy_price and price < sell_price:
                            best_buy_price = price
                            best_buy_station = station_name
                            best_buy_volume = vol
                        elif price == best_buy_price and vol > best_buy_volume:
                            best_buy_price = price
                            best_buy_station = station_name
                            best_buy_volume = vol

        if best_buy_price == float('inf'):
            continue

        # Calculate margin (before tax)
        margin_pct = ((sell_price - best_buy_price) / best_buy_price) * 100 if best_buy_price > 0 else 0

        if margin_pct < margin:
            continue

        # Calculate quantities
        item_vol = type_info.get(tid, {}).get('volume', 1.0)
        max_quantity_by_vol = int(capacity / item_vol)
        max_quantity = min(max_quantity_by_vol, best_buy_volume)

        if max_quantity <= 0:
            continue

        # Calculate costs and revenue with tax
        buy_cost = best_buy_price * max_quantity
        tax_on_buy = buy_cost * sales_tax
        total_buy_cost = buy_cost + tax_on_buy

        # Selling revenue after tax
        sell_revenue = sell_price * max_quantity
        tax_on_sell = sell_revenue * sales_tax
        net_sell_revenue = sell_revenue - tax_on_sell

        # Calculate competition for sell side in hub
        sell_competition = calculate_sell_competition_hub(hub, tid, 1440)

        profit = net_sell_revenue - total_buy_cost

        # Record purchase
        if best_buy_station not in purchases:
            purchases[best_buy_station] = []
        purchases[best_buy_station].append({
            'item': name,
            'quantity': max_quantity,
            'total': round(buy_cost, 2)
        })

        # Record sale
        sales[name] = {
            'sell_price': sell_price,
            'total': max_quantity,
            'volume': round(max_quantity * item_vol, 2),
            'revenue': round(net_sell_revenue, 2),
            'competition': {
                '5_minutes': calculate_sell_competition_hub(hub, tid, 5),
                '1_hour': calculate_sell_competition_hub(hub, tid, 60),
                '1_day': sell_competition
            }
        }

        total_cost += total_buy_cost
        total_revenue += net_sell_revenue
        total_volume += max_quantity * item_vol

    # Sort purchases by station
    for station in purchases:
        purchases[station].sort(key=lambda x: x['item'])

    return JSONResponse(status_code=200, content={
        'cost': round(total_cost, 2),
        'revenue': round(total_revenue, 2),
        'profit': round(total_revenue - total_cost, 2),
        'purchases': purchases,
        'sales': sales
    })


@app.post("/v1/profit/trade")
async def profit_trade_endpoint(request: Request, x_api_key: str | None = Header(None, alias="X-API-Key")):
    """
    Find items to trade inside a hub (buy and sell in same hub).
    """
    body = await request.json()

    hub = body.get('hub', 'jita').lower()
    margin = body.get('margin', 20.0)
    sorting_by = body.get('sorting_by', 'ppu')
    k = body.get('k', 5)
    daily_traded = body.get('daily_traded', 1e8)
    daily_volume = body.get('daily_volume', 5.0)
    competition_window = body.get('competition_window', 1440)
    buy_competition = body.get('buy_competition', 3)
    sell_competition = body.get('sell_competition', 3)
    sales_tax = body.get('sales_tax', 0.04)

    # Validate hub
    valid_hubs = set(MAIN_HUBS.keys())
    if hub not in valid_hubs:
        raise HTTPException(status_code=400, detail={"error": "INVALID_REQUEST", "details": f"Invalid hub: {hub}. Valid hubs: {list(valid_hubs)}"})

    # Get effective timestamp
    update_effective_timestamp()

    hub_station_id = MAIN_HUBS[hub]

    # Get region for the hub
    region_map = {
        'jita': 10000002,
        'amarr': 10000003,
        'dodixie': 10000004,
        'rens': 10000031,
        'hek': 10000032
    }
    region_id = region_map.get(hub, 10000002)

    results = {}

    for tid, orders in price_books.get(hub, {}).items():
        name = type_names.get(tid, f'Type {tid}')

        # Get buy and sell orders
        buy_orders = [o for o in orders.values() if isinstance(o, dict) and o.get('is_buy_order', False)]
        sell_orders = [o for o in orders.values() if isinstance(o, dict) and not o.get('is_buy_order', False)]

        if not buy_orders or not sell_orders:
            continue

        # Get best prices
        best_buy = max(buy_orders, key=lambda x: x.get('price', 0))
        best_sell = min(sell_orders, key=lambda x: x.get('price', float('inf')))

        buy_price = best_buy.get('price', 0)
        sell_price = best_sell.get('price', 0)

        if buy_price <= 0 or sell_price <= 0:
            continue

        # Calculate margin (before tax)
        margin_pct = ((sell_price - buy_price) / buy_price) * 100

        if margin_pct < margin:
            continue

        # Calculate competition
        buy_comp = calculate_buy_competition_hub(hub, tid, competition_window)
        sell_comp = calculate_sell_competition_hub(hub, tid, competition_window)

        if buy_comp > buy_competition or sell_comp > sell_competition:
            continue

        # Get average daily stats
        avg_daily_value, avg_daily_vol = get_average_daily_stats(region_id, tid)

        if avg_daily_value < daily_traded or avg_daily_vol < daily_volume:
            continue

        # Calculate profit per unit (before tax)
        ppu = sell_price - buy_price

        # Daily profit: 10% of avg daily volume * ppu * (1 - sales_tax)
        daily_profit = (avg_daily_vol * 0.10) * ppu * (1 - sales_tax)

        # Determine buy/sell prices we should set
        # For buy: undercut the best buy order by at least 4 digits
        our_buy_price = round_price_4_digits(buy_price - 1)  # Undercut by 1 ISK then round

        # For sell: undercut the best sell order by at least 4 digits
        our_sell_price = round_price_4_digits(sell_price - 1)  # Undercut by 1 ISK then round

        results[name] = {
            'margin': round(margin_pct, 2),
            'value': round(avg_daily_value, 2),
            'volume': round(avg_daily_vol, 2),
            'ppu': round(ppu, 2),
            'buy_competition': buy_comp,
            'sell_competition': sell_comp,
            'buy_price': our_buy_price,
            'sell_price': our_sell_price,
            'daily_profit': round(daily_profit, 2)
        }

    # Sort by sorting_by parameter
    if sorting_by == 'margin':
        sorted_items = sorted(results.items(), key=lambda x: (-x[1]['margin'], x[0]))
    elif sorting_by == 'daily':
        sorted_items = sorted(results.items(), key=lambda x: (-x[1]['value'], x[0]))
    else:  # ppu
        sorted_items = sorted(results.items(), key=lambda x: (-x[1]['ppu'], x[0]))

    # Limit to k items
    sorted_items = sorted_items[:k]

    return JSONResponse(status_code=200, content=dict(sorted_items))


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
