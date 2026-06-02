#!/usr/bin/env python3
"""Build an in-memory cache of SDE reprocessing data for faster lookups."""
import bz2
import csv
import json
import os

SDE_PATH = "/workspace/sde"
CACHE_FILE = "/workspace/sde_cache.json"


def load_sde_data() -> dict:
    cache = {"type_names": {}, "type_info": {}, "type_materials": {}, "group_hierarchy": {}, "ref_skill_map": {}}

    def read_int(row, key):
        try:
            return int(row[key])
        except (ValueError, KeyError):
            return None

    print("Loading invTypes...")
    with bz2.open(f"{SDE_PATH}/invTypes.csv.bz2", "rt") as f:
        for row in csv.DictReader(f):
            tid = read_int(row, "typeID")
            if tid:
                name = row["typeName"]
                gid = read_int(row, "groupID") or 0
                volume = row.get("volume", "")
                cache["type_names"][name] = tid
                cache["type_info"][tid] = {"group_id": gid, "volume": volume, "portionSize": row.get("portionSize", "")}
                if mass := row.get("mass"):
                    cache["type_info"][tid]["mass"] = mass

    print("Loading invTypeMaterials...")
    with bz2.open(f"{SDE_PATH}/invTypeMaterials.csv.bz2", "rt") as f:
        for row in csv.DictReader(f):
            tid = read_int(row, "typeID")
            mat_tid = read_int(row, "materialTypeID")
            qty = read_int(row, "quantity")
            if tid and mat_tid and qty:
                cache.setdefault("type_materials", {}).setdefault(tid, []).append((mat_tid, qty))

    print("Loading invGroups...")
    with bz2.open(f"{SDE_PATH}/invGroups.csv.bz2", "rt") as f:
        for row in csv.DictReader(f):
            gid = read_int(row, "groupID")
            if gid:
                cache["group_hierarchy"][gid] = {"category_id": read_int(row, "categoryID") or 0, "name": row.get("groupName", "")}

    print("Loading dgmTypeAttributes for refSkill mapping...")
    with bz2.open(f"{SDE_PATH}/dgmTypeAttributes.csv.bz2", "rt") as f:
        for row in csv.DictReader(f):
            tid = read_int(row, "typeID")
            aid = read_int(row, "attributeID")
            if tid and aid == 1288 and (vfloat := row.get("valueFloat")):
                cache["ref_skill_map"][tid] = int(float(vfloat))

    print("Saving cache...")
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)

    print(f"Cache saved to {CACHE_FILE}\nType names: {len(cache['type_names'])}\nType info: {len(cache['type_info'])}\nType materials: {len(cache['type_materials'])}")
    return cache


if __name__ == "__main__":
    load_sde_data()
