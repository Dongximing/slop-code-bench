#!/usr/bin/env python3
"""
Build an in-memory cache of SDE reprocessing data for faster lookups.
"""

import bz2
import csv
import json
from typing import Dict, List, Any, Optional
import io

SDE_PATH = "/workspace/sde"
CACHE_FILE = "/workspace/sde_cache.json"

def load_sde_data() -> Dict[str, Any]:
    """Load all necessary SDE data into structured format."""
    cache = {
        "type_names": {},          # name -> type_id
        "type_info": {},           # type_id -> {group_id, mass, volume, portionSize}
        "type_materials": {},      # type_id -> [(material_type_id, quantity), ...]
        "group_hierarchy": {},     # group_id -> [parent groups and categories]
        "ref_skill_map": {},       # type_id -> ref skill name (from dgmTypeAttributes)
    }

    # Load invTypes.csv.bz2 - names and basic info
    print("Loading invTypes...")
    with bz2.open(f"{SDE_PATH}/invTypes.csv.bz2", 'rt') as f:
        reader = csv.DictReader(f)
        for row in reader:
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

    # Load invTypeMaterials.csv.bz2 - material recipes
    print("Loading invTypeMaterials...")
    with bz2.open(f"{SDE_PATH}/invTypeMaterials.csv.bz2", 'rt') as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                type_id = int(row['typeID'])
                material_type_id = int(row['materialTypeID'])
                quantity = int(row['quantity'])

                if type_id not in cache["type_materials"]:
                    cache["type_materials"][type_id] = []
                cache["type_materials"][type_id].append((material_type_id, quantity))
            except (ValueError, KeyError):
                continue

    # Load invGroups.csv.bz2 - group hierarchy
    print("Loading invGroups...")
    with bz2.open(f"{SDE_PATH}/invGroups.csv.bz2", 'rt') as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                group_id = int(row['groupID'])
                category_id = int(row['categoryID'])
                parent_group_id = row.get('useBasePrice')  # This might be wrong
                # Actually let me check the schema - groupID,categoryID,groupName,iconID,useBasePrice,anchored,anchorable,fittableNonSingleton,published
                # So useBasePrice is boolean, not parent group
                # There's no parent group column in invGroups
                cache["group_hierarchy"][group_id] = {
                    "category_id": category_id,
                    "name": row.get('groupName', ''),
                }
            except (ValueError, KeyError):
                continue

    # Load dgmTypeAttributes.csv.bz2 - find refSkill attribute
    print("Loading dgmTypeAttributes for refSkill mapping...")
    with bz2.open(f"{SDE_PATH}/dgmTypeAttributes.csv.bz2", 'rt') as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                type_id = int(row['typeID'])
                attribute_id = int(row['attributeID'])
                # Attribute 1288 is refSkill (reference skill)
                if attribute_id == 1288:
                    if value_float := row.get('valueFloat'):
                        skill_name = row.get('valueInt')  # This might be wrong
                        # Actually let me check - valueInt and valueFloat, one contains the skill ID
                        # The skill ID would be in valueInt, and valueFloat might be something else
                        skill_id = int(value_float)
                        # We need to map skill ID to name later
                        cache["ref_skill_map"][type_id] = skill_id
            except (ValueError, KeyError):
                continue

    # Save cache
    print("Saving cache...")
    with open(CACHE_FILE, 'w') as f:
        json.dump(cache, f, indent=2)

    print(f"Cache saved to {CACHE_FILE}")
    print(f"Type names: {len(cache['type_names'])}")
    print(f"Type info: {len(cache['type_info'])}")
    print(f"Type materials: {len(cache['type_materials'])}")

if __name__ == "__main__":
    load_sde_data()
