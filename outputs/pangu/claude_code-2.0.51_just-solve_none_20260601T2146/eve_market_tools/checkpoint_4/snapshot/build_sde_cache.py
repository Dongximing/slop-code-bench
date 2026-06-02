#!/usr/bin/env python3
"""Build an SDE cache for reprocessing."""
import bz2
import csv
import json
import os
from typing import Any

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SDE_PATH = os.path.join(SCRIPT_DIR, "sde")
CACHE_FILE = os.path.join(SCRIPT_DIR, "sde_cache.json")


def build_cache():
    cache = {"types": {}, "materials": {}, "groups": {}, "categories": {}, "type_to_name": {}, "attributes": {}, "skill_names": {}}

    def _load_csv(filepath):
        try:
            return bz2.open(filepath, "rt", encoding="utf-8")
        except Exception:
            return None

    def _read_int(row, key):
        try:
            return int(row[key])
        except (ValueError, KeyError):
            return None

    def _read_float(row, key):
        try:
            return float(row[key])
        except (ValueError, KeyError):
            return None

    print("Loading categories...")
    with _load_csv(f"{SDE_PATH}/invCategories.csv.bz2") as f:
        if f:
            for row in csv.DictReader(f):
                if cid := _read_int(row, "categoryID"):
                    cache["categories"][cid] = row["categoryName"]

    print("Loading groups...")
    with _load_csv(f"{SDE_PATH}/invGroups.csv.bz2") as f:
        if f:
            for row in csv.DictReader(f):
                gid = _read_int(row, "groupID")
                if gid is None:
                    continue
                cache["groups"][gid] = {"name": row.get("groupName", ""), "category_id": _read_int(row, "categoryID") or 0}

    print("Loading types...")
    with _load_csv(f"{SDE_PATH}/invTypes.csv.bz2") as f:
        if f:
            for row in csv.DictReader(f):
                tid = _read_int(row, "typeID")
                if tid is None:
                    continue
                name = row["typeName"]
                cache["types"][tid] = {"name": name, "group_id": _read_int(row, "groupID") or 0, "volume": _read_float(row, "volume") or 0, "portion_size": _read_int(row, "portionSize") or 0}
                cache["type_to_name"][name.lower()] = tid

    print("Loading materials...")
    with _load_csv(f"{SDE_PATH}/invTypeMaterials.csv.bz2") as f:
        if f:
            for row in csv.DictReader(f):
                tid = _read_int(row, "typeID")
                mat_tid = _read_int(row, "materialTypeID")
                qty = _read_int(row, "quantity")
                if tid and mat_tid and qty:
                    cache.setdefault("materials", {}).setdefault(tid, []).append((mat_tid, qty))

    print("Loading dgmTypeAttributes...")
    with _load_csv(f"{SDE_PATH}/dgmTypeAttributes.csv.bz2") as f:
        if f:
            for row in csv.DictReader(f):
                tid = _read_int(row, "typeID")
                aid = _read_int(row, "attributeID")
                if tid and aid:
                    if vfloat := row.get("valueFloat"):
                        cache.setdefault("attributes", {}).setdefault(tid, {})[aid] = float(vfloat)
                    elif vint := row.get("valueInt"):
                        cache.setdefault("attributes", {}).setdefault(tid, {})[aid] = int(vint)

    print("Saving cache...")
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)

    ore_types = ["Plagioclase", "Kernite", "Arkonor", "Spodumain", "Hedbergite", "Omber"]
    print(f"\n{'='*50}\nSDE Cache Statistics:\n  Types: {len(cache['types'])}\n  Materials: {len(cache['materials'])}\n  Groups: {len(cache['groups'])}\n  Categories: {len(cache['categories'])}\n  Attributes: {len(cache['attributes'])}\n{'='*50}")
    print("\nOre material recipes:")
    for name in ore_types:
        tid = cache["type_to_name"].get(name.lower())
        if tid and tid in cache["materials"]:
            print(f"  {name} ({tid}): {cache['materials'][tid]}")
        else:
            print(f"  {name}: No materials data")


if __name__ == "__main__":
    build_cache()
