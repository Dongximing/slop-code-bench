#!/usr/bin/env python3
"""
Build a properly formatted SDE cache for the reprocessing API.
"""

import bz2
import gzip
import csv
import json
import os
from typing import Dict, List, Any, Optional, Tuple
import io

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SDE_PATH = os.path.join(SCRIPT_DIR, "sde")
CACHE_FILE = os.path.join(SCRIPT_DIR, "sde_cache.json")

def open_any(filepath: str):
    """Open as bz2, gzip, or plain text."""
    for opener in (lambda p: bz2.open(p, 'rt', encoding='utf-8'),
                   lambda p: gzip.open(p, 'rt', encoding='utf-8'),
                   lambda p: open(p, 'r', encoding='utf-8'))):
        try:
            return opener(filepath)
        except Exception:
            pass
    return None

def build_cache():
    """Build the SDE cache in the format expected by market_tools.py."""
    cache = {}

    # Load invTypes.csv.bz2 - names, group_ids, volumes, portion_sizes
    print("Loading types from invTypes.csv.bz2...")
    types_data = {}
    with open_any(f"{SDE_PATH}/invTypes.csv.bz2") as f:
        if f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    tid = int(row['typeID'])
                    name = row['typeName']
                    volume = float(row['volume'])
                    portion_size = int(row['portionSize'])
                    group_id = int(row['groupID'])

                    types_data[name.lower()] = tid
                    cache[str(tid)] = {
                        "name": name,
                        "group_id": group_id,
                        "volume": volume,
                        "portion_size": portion_size
                    }
                except (ValueError, KeyError):
                    continue

    # Load invTypeMaterials.csv.bz2 - material recipes
    print("Loading materials from invTypeMaterials.csv.bz2...")
    materials_data = {}
    with open_any(f"{SDE_PATH}/invTypeMaterials.csv.bz2") as f:
        if f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    tid = str(int(row['typeID']))
                    mat_tid = str(int(row['materialTypeID']))
                    qty = int(row['quantity'])

                    if tid not in materials_data:
                        materials_data[tid] = []
                    materials_data[tid].append((mat_tid, qty))
                except (ValueError, KeyError):
                    continue

    cache["materials"] = materials_data

    # Load invGroups.csv.bz2 - group to category mapping
    print("Loading groups from invGroups.csv.bz2...")
    groups_data = {}
    with open_any(f"{SDE_PATH}/invGroups.csv.bz2") as f:
        if f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    gid = str(int(row['groupID']))
                    gid_int = int(gid)
                    cat_id = int(row['categoryID'])
                    groups_data[gid] = {
                        "name": row.get('groupName', ''),
                        "category_id": cat_id
                    }
                except (ValueError, KeyError):
                    continue

    cache["groups"] = groups_data

    # Load invCategories.csv.bz2 - category names
    print("Loading categories from invCategories.csv.bz2...")
    categories_data = {}
    with open_any(f"{SDE_PATH}/invCategories.csv.bz2") as f:
        if f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    cid = str(int(row['categoryID']))
                    categories_data[cid] = row['categoryName']
                except (ValueError, KeyError):
                    continue

    cache["categories"] = categories_data

    # Add types data under 'types' key for easier access
    cache["types"] = cache  # This creates a nested structure, but we want flat

    # Actually, let's reorganize: types should be a flat dict with tid -> info, name -> tid
    types_flat = {}
    for tid_str, info in cache.items():
        if tid_str not in ["materials", "groups", "categories"]:
            types_flat[tid_str] = info

    cache["types"] = types_flat

    # Save cache
    print(f"\nSaving cache to {CACHE_FILE}...")
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, indent=2)

    print(f"Cache saved! Stats:")
    print(f"  Types: {len(cache['types'])}")
    print(f"  Materials entries: {len(cache.get('materials', {}))}")
    print(f"  Groups: {len(cache.get('groups', {}))}")

    # Test lookup
    print("\nTest lookups:")
    # Find Plagioclase
    plagioclase_tid = None
    for tid, info in cache['types'].items():
        if info['name'].lower() == 'plagioclase':
            plagioclase_tid = tid
            print(f"  Plagioclase: {tid} -> {info['group_id']}")
            break

    if plagioclase_tid and plagioclase_tid in cache.get('materials', {}):
        print(f"  Materials: {cache['materials'][plagioclase_tid]}")

if __name__ == "__main__":
    build_cache()
