#!/usr/bin/env python3
"""
Build a comprehensive SDE cache for reprocessing.
"""

import bz2
import gzip
import csv
import json
import io
import argparse
from typing import Dict, List, Any, Optional, Tuple

# SDE path is relative to this script location
import os
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SDE_PATH = os.path.join(SCRIPT_DIR, "sde")
CACHE_FILE = os.path.join(SCRIPT_DIR, "sde_cache.json")

# Known group IDs for categorization
ORE_GROUPS = {454, 457, 458, 461, 463, 480, 115, 116}  # Various ore groups
ICE_GROUPS = {465, 423}  # Ice, Ice products
GAS_GROUPS = {422, 1033, 1273, 1274, 1275, 1276, 1277, 1278, 1279}  # Gas isotoeps, etc.
COMPRESSED_ORE_GROUPS = {884, 903}  # Compressed variants
COMPRESSED_ICE_GROUPS = {903}  # Ancient Compressed Ice

# Known skill IDs (from trnTranslation or directly)
SKILL_NAMES = {
    11329: "Reprocessing",
    11332: "Reprocessing Efficiency",
    # Add more as needed - we'll populate from dgmAttributeTypes if available
}

def open_any(filepath: str):
    """Open as bz2, gzip, or plain text."""
    for opener in (lambda p: bz2.open(p, 'rt', encoding='utf-8'),
                   lambda p: gzip.open(p, 'rt', encoding='utf-8'),
                   lambda p: open(p, 'r', encoding='utf-8')):
        try:
            return opener(filepath)
        except Exception:
            pass
    return None

def load_csv(filepath: str) -> csv.DictReader:
    """Load a CSV file and return a DictReader."""
    f = open_any(filepath)
    if f:
        return csv.DictReader(f)
    return []

def build_cache():
    """Build the complete SDE cache."""
    cache = {
        "types": {},          # type_id -> {name, group_id, category_id, volume, portion_size}
        "materials": {},      # type_id -> [(material_type_id, quantity), ...]
        "groups": {},         # group_id -> {name, category_id}
        "categories": {},     # category_id -> name
        "type_to_name": {},   # name -> type_id (lowercase)
        "attributes": {},     # type_id -> {attribute_id: value}
        "skill_names": {},    # skill_id -> name
    }

    # Load categories (invCategories.csv.bz2)
    print("Loading categories...")
    try:
        with open_any(f"{SDE_PATH}/invCategories.csv.bz2") as f:
            if f:
                for row in csv.DictReader(f):
                    cache["categories"][int(row['categoryID'])] = row['categoryName']
    except Exception as e:
        print(f"Error loading categories: {e}")

    # Load groups (invGroups.csv.bz2)
    print("Loading groups...")
    try:
        with open_any(f"{SDE_PATH}/invGroups.csv.bz2") as f:
            if f:
                for row in csv.DictReader(f):
                    gid = int(row['groupID'])
                    cache["groups"][gid] = {
                        "name": row.get('groupName', ''),
                        "category_id": int(row['categoryID']),
                    }
    except Exception as e:
        print(f"Error loading groups: {e}")

    # Load types (invTypes.csv.bz2)
    print("Loading types...")
    with open_any(f"{SDE_PATH}/invTypes.csv.bz2") as f:
        if f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    tid = int(row['typeID'])
                    cache["types"][tid] = {
                        "name": row['typeName'],
                        "group_id": int(row['groupID']),
                        "volume": float(row['volume']),
                        "portion_size": int(row['portionSize']),
                    }
                    # Index by name (lowercase for case-insensitive lookup)
                    cache["type_to_name"][row['typeName'].lower()] = tid
                except (ValueError, KeyError):
                    continue

    # Load materials (invTypeMaterials.csv.bz2)
    print("Loading materials...")
    with open_any(f"{SDE_PATH}/invTypeMaterials.csv.bz2") as f:
        if f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    tid = int(row['typeID'])
                    mat_tid = int(row['materialTypeID'])
                    qty = int(row['quantity'])
                    if tid not in cache["materials"]:
                        cache["materials"][tid] = []
                    cache["materials"][tid].append((mat_tid, qty))
                except (ValueError, KeyError):
                    continue

    # Load dgmTypeAttributes (dgmTypeAttributes.csv.bz2)
    # Looking for refSkill (attribute 1288) and reprocessing yield (attribute 1162? or 175?)
    print("Loading dgmTypeAttributes...")
    with open_any(f"{SDE_PATH}/dgmTypeAttributes.csv.bz2") as f:
        if f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    tid = int(row['typeID'])
                    aid = int(row['attributeID'])
                    if tid not in cache["attributes"]:
                        cache["attributes"][tid] = {}
                    if row.get('valueFloat'):
                        cache["attributes"][tid][aid] = float(row['valueFloat'])
                    elif row.get('valueInt'):
                        cache["attributes"][tid][aid] = int(row['valueInt'])
                except (ValueError, KeyError):
                    continue

    # Also try to load dgmAttributeTypes for skill name mapping
    print("Loading dgmAttributeTypes for skill names...")
    try:
        with open_any(f"{SDE_PATH}/dgmAttributeTypes.csv.bz2") as f:
            if f:
                for row in csv.DictReader(f):
                    # This maps attribute IDs to names
                    # We're looking for skill-related attributes
                    pass
    except:
        pass

    # Try to load trnTranslation for skill names
    print("Loading trnTranslation for skill names...")
    try:
        with open_any(f"{SDE_PATH}/trnTranslation.csv.bz2") as f:
            if f:
                for row in csv.DictReader(f):
                    # Format: tableID, keyID, valueID, textID, text
                    # Or similar - need to check actual structure
                    pass
    except:
        pass

    # Save cache
    print("Saving cache...")
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)

    # Print stats
    print(f"\n{'='*50}")
    print("SDE Cache Statistics:")
    print(f"  Types: {len(cache['types'])}")
    print(f"  Materials: {len(cache['materials'])}")
    print(f"  Groups: {len(cache['groups'])}")
    print(f"  Categories: {len(cache['categories'])}")
    print(f"  Attributes: {len(cache['attributes'])}")
    print(f"  Type names: {len(cache['type_to_name'])}")
    print(f"{'='*50}")

    # Show a few examples
    print("\nExample types found:")
    for name, tid in list(cache["type_to_name"].items())[:10]:
        print(f"  {tid}: {name}")

    # Show ore materials example
    ore_types = ["Plagioclase", "Kernite", "Arkonor", "Spodumain", "Hedbergite", "Omber"]
    print("\nOre material recipes:")
    for ore_name in ore_types:
        tid = cache["type_to_name"].get(ore_name.lower())
        if tid and tid in cache["materials"]:
            mats = cache["materials"][tid]
            print(f"  {ore_name} ({tid}): {mats}")
        else:
            print(f"  {ore_name}: No materials data")

if __name__ == "__main__":
    build_cache()
