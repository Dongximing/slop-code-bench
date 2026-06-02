"""
Market data and SDE loading for the Reprocessing API.
"""
import csv
import gzip
import json
from collections import defaultdict
from typing import Any, Dict, Optional

price_books: Dict[str, Dict[int, Dict[str, Any]]] = defaultdict(dict)
stations_data: Dict[int, Dict[str, Any]] = {}

type_names: Dict[int, str] = {}
type_names_lower: Dict[str, int] = {}
type_info: Dict[int, Dict[str, Any]] = {}
type_materials: Dict[int, list] = {}
groups: Dict[int, Dict[str, Any]] = {}
categories: Dict[int, str] = {}
group_categories: Dict[int, int] = {}
api_keys: Dict[str, Dict[str, Any]] = {}
sde_loaded = False


def open_any(filepath: str):
    for opener in (
        lambda p: gzip.open(p, "rt", encoding="utf-8"),
        lambda p: open(p, "r", encoding="utf-8"),
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
        with open(cache_file, "r") as f:
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
                    tid = int(row["typeID"])
                    name = row["typeName"]
                    type_names[tid] = name
                    type_names_lower[name.lower()] = tid
                    type_info[tid] = {
                        "group_id": int(row["groupID"]),
                        "volume": float(row["volume"]),
                        "portion_size": int(row["portionSize"]),
                    }
                except (ValueError, KeyError):
                    continue

    with open_any(f"{sde_path}/invTypeMaterials.csv.bz2") as f:
        if f:
            for row in csv.DictReader(f):
                try:
                    tid = int(row["typeID"])
                    mat_tid = int(row["materialTypeID"])
                    qty = int(row["quantity"])
                    type_materials.setdefault(tid, []).append((mat_tid, qty))
                except (ValueError, KeyError):
                    continue

    with open_any(f"{sde_path}/invGroups.csv.bz2") as f:
        if f:
            for row in csv.DictReader(f):
                try:
                    gid = int(row["groupID"])
                    cid = int(row["categoryID"])
                    groups[gid] = {"name": row.get("groupName", ""), "category_id": cid}
                    group_categories[gid] = cid
                except (ValueError, KeyError):
                    continue

    with open_any(f"{sde_path}/invCategories.csv.bz2") as f:
        if f:
            for row in csv.DictReader(f):
                try:
                    cid = int(row["categoryID"])
                    categories[cid] = row["categoryName"]
                except (ValueError, KeyError):
                    continue

    sde_loaded = True
    print(f"SDE loaded: {len(type_names_lower)} types, {len(type_materials)} materials")
