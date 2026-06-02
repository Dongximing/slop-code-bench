#!/usr/bin/env python3
"""Build an in-memory cache of SDE reprocessing data for faster lookups."""

import bz2
import csv
import json
from typing import Any

SDE_PATH = "/workspace/sde"
CACHE_FILE = "/workspace/sde_cache.json"

def load_sde_data() -> dict[str, Any]:
    cache = {
        "type_names": {},
        "type_info": {},
        "type_materials": {},
        "group_hierarchy": {},
        "ref_skill_map": {},
    }

    print("Loading invTypes...")
    with bz2.open(f"{SDE_PATH}/invTypes.csv.bz2", 'rt') as f:
        for row in csv.DictReader(f):
            try:
                type_id = int(row['typeID'])
                name = row['typeName']
                group_id = int(row['groupID'])
                volume = row['volume']
                portion_size = row['portionSize']

                cache["type_names"][name] = type_id
                cache["type_info"][type_id] = {
                    "group_id": group_id,
                    "volume": volume,
                    "portionSize": portion_size,
                }
                if mass := row.get('mass'):
                    cache["type_info"][type_id]["mass"] = mass
            except (ValueError, KeyError):
                continue

    print("Loading invTypeMaterials...")
    with bz2.open(f"{SDE_PATH}/invTypeMaterials.csv.bz2", 'rt') as f:
        for row in csv.DictReader(f):
            try:
                type_id = int(row['typeID'])
                material_type_id = int(row['materialTypeID'])
                quantity = int(row['quantity'])

                cache["type_materials"].setdefault(type_id, []).append((material_type_id, quantity))
            except (ValueError, KeyError):
                continue

    print("Loading invGroups...")
    with bz2.open(f"{SDE_PATH}/invGroups.csv.bz2", 'rt') as f:
        for row in csv.DictReader(f):
            try:
                group_id = int(row['groupID'])
                category_id = int(row['categoryID'])
                cache["group_hierarchy"][group_id] = {
                    "category_id": category_id,
                    "name": row.get('groupName', ''),
                }
            except (ValueError, KeyError):
                continue

    print("Loading dgmTypeAttributes for refSkill mapping...")
    with bz2.open(f"{SDE_PATH}/dgmTypeAttributes.csv.bz2", 'rt') as f:
        for row in csv.DictReader(f):
            try:
                type_id = int(row['typeID'])
                attribute_id = int(row['attributeID'])
                if attribute_id == 1288:
                    if value_float := row.get('valueFloat'):
                        skill_id = int(float(value_float))
                        cache["ref_skill_map"][type_id] = skill_id
            except (ValueError, KeyError):
                continue

    print("Saving cache...")
    with open(CACHE_FILE, 'w') as f:
        json.dump(cache, f, indent=2)

    print(f"Cache saved to {CACHE_FILE}")
    print(f"Type names: {len(cache['type_names'])}")
    print(f"Type info: {len(cache['type_info'])}")
    print(f"Type materials: {len(cache['type_materials'])}")

if __name__ == "__main__":
    load_sde_data()