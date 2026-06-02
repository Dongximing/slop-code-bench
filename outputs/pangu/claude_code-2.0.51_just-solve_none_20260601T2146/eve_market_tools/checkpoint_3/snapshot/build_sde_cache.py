#!/usr/bin/env python3
"""Build a comprehensive SDE cache for reprocessing."""

import bz2
import csv
import json
import os
from typing import Dict, Any

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SDE_PATH = os.path.join(SCRIPT_DIR, "sde")
CACHE_FILE = os.path.join(SCRIPT_DIR, "sde_cache.json")

def _load_csv(filepath: str):
    try:
        return bz2.open(filepath, 'rt', encoding='utf-8')
    except Exception:
        return None

def build_cache():
    cache = {
        "types": {}, "materials": {}, "groups": {},
        "categories": {}, "type_to_name": {},
        "attributes": {}, "skill_names": {}
    }

    print("Loading categories...")
    with _load_csv(f"{SDE_PATH}/invCategories.csv.bz2") as f:
        if f:
            for row in csv.DictReader(f):
                cache["categories"][int(row['categoryID'])] = row['categoryName']

    print("Loading groups...")
    with _load_csv(f"{SDE_PATH}/invGroups.csv.bz2") as f:
        if f:
            for row in csv.DictReader(f):
                gid = int(row['groupID'])
                cache["groups"][gid] = {
                    "name": row.get('groupName', ''),
                    "category_id": int(row['categoryID']),
                }

    print("Loading types...")
    with _load_csv(f"{SDE_PATH}/invTypes.csv.bz2") as f:
        if f:
            for row in csv.DictReader(f):
                try:
                    tid = int(row['typeID'])
                    cache["types"][tid] = {
                        "name": row['typeName'],
                        "group_id": int(row['groupID']),
                        "volume": float(row['volume']),
                        "portion_size": int(row['portionSize']),
                    }
                    cache["type_to_name"][row['typeName'].lower()] = tid
                except (ValueError, KeyError):
                    continue

    print("Loading materials...")
    with _load_csv(f"{SDE_PATH}/invTypeMaterials.csv.bz2") as f:
        if f:
            for row in csv.DictReader(f):
                try:
                    tid = int(row['typeID'])
                    mat_tid = int(row['materialTypeID'])
                    qty = int(row['quantity'])
                    cache["materials"].setdefault(tid, []).append((mat_tid, qty))
                except (ValueError, KeyError):
                    continue

    print("Loading dgmTypeAttributes...")
    with _load_csv(f"{SDE_PATH}/dgmTypeAttributes.csv.bz2") as f:
        if f:
            for row in csv.DictReader(f):
                try:
                    tid = int(row['typeID'])
                    aid = int(row['attributeID'])
                    cache["attributes"].setdefault(tid, {})
                    if row.get('valueFloat'):
                        cache["attributes"][tid][aid] = float(row['valueFloat'])
                    elif row.get('valueInt'):
                        cache["attributes"][tid][aid] = int(row['valueInt'])
                except (ValueError, KeyError):
                    continue

    print("Saving cache...")
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)

    print(f"\n{'='*50}")
    print("SDE Cache Statistics:")
    print(f"  Types: {len(cache['types'])}")
    print(f"  Materials: {len(cache['materials'])}")
    print(f"  Groups: {len(cache['groups'])}")
    print(f"  Categories: {len(cache['categories'])}")
    print(f"  Attributes: {len(cache['attributes'])}")
    print(f"{'='*50}")

    ore_types = ["Plagioclase", "Kernite", "Arkonor", "Spodumain", "Hedbergite", "Omber"]
    print("\nOre material recipes:")
    for ore_name in ore_types:
        tid = cache["type_to_name"].get(ore_name.lower())
        if tid and tid in cache["materials"]:
            print(f"  {ore_name} ({tid}): {cache['materials'][tid]}")
        else:
            print(f"  {ore_name}: No materials data")

if __name__ == "__main__":
    build_cache()