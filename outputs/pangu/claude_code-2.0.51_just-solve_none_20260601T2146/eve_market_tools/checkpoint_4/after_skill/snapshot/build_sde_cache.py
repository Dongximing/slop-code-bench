#!/usr/bin/env python3
"""Build an SDE cache for reprocessing."""
import bz2
import csv
import json
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SDE_PATH = os.path.join(SCRIPT_DIR, "sde")
CACHE_FILE = os.path.join(SCRIPT_DIR, "sde_cache.json")


def int_or_none(row, key):
    try:
        return int(row[key])
    except (ValueError, KeyError):
        return None


def float_or_none(row, key):
    try:
        return float(row[key])
    except (ValueError, KeyError):
        return None


def build_cache():
    cache = {"types": {}, "materials": {}, "groups": {}, "categories": {}, "type_to_name": {}, "attributes": {}, "skill_names": {}}

    def load_csv(filepath):
        try:
            return bz2.open(filepath, "rt", encoding="utf-8")
        except Exception:
            return None

    print("Loading categories...")
    with load_csv(f"{SDE_PATH}/invCategories.csv.bz2") as f:
        if f:
            for row in csv.DictReader(f):
                cid = int_or_none(row, "categoryID")
                if cid is not None:
                    cache["categories"][cid] = row["categoryName"]

    print("Loading groups...")
    with load_csv(f"{SDE_PATH}/invGroups.csv.bz2") as f:
        if f:
            for row in csv.DictReader(f):
                gid = int_or_none(row, "groupID")
                if gid is not None:
                    cache["groups"][gid] = {"name": row.get("groupName", ""), "category_id": int_or_none(row, "categoryID") or 0}

    print("Loading types...")
    with load_csv(f"{SDE_PATH}/invTypes.csv.bz2") as f:
        if f:
            for row in csv.DictReader(f):
                tid = int_or_none(row, "typeID")
                if tid is not None:
                    name = row["typeName"]
                    cache["types"][tid] = {
                        "name": name,
                        "group_id": int_or_none(row, "groupID") or 0,
                        "volume": float_or_none(row, "volume") or 0,
                        "portion_size": int_or_none(row, "portionSize") or 0,
                    }
                    cache["type_to_name"][name.lower()] = tid

    print("Loading materials...")
    with load_csv(f"{SDE_PATH}/invTypeMaterials.csv.bz2") as f:
        if f:
            for row in csv.DictReader(f):
                tid = int_or_none(row, "typeID")
                mat_tid = int_or_none(row, "materialTypeID")
                qty = int_or_none(row, "quantity")
                if tid is not None and mat_tid is not None and qty is not None:
                    cache.setdefault("materials", {}).setdefault(tid, []).append((mat_tid, qty))

    print("Loading dgmTypeAttributes...")
    with load_csv(f"{SDE_PATH}/dgmTypeAttributes.csv.bz2") as f:
        if f:
            for row in csv.DictReader(f):
                tid = int_or_none(row, "typeID")
                aid = int_or_none(row, "attributeID")
                if tid is not None and aid is not None:
                    vfloat = row.get("valueFloat")
                    if vfloat:
                        cache.setdefault("attributes", {}).setdefault(tid, {})[aid] = float(vfloat)
                    else:
                        vint = row.get("valueInt")
                        if vint:
                            cache.setdefault("attributes", {}).setdefault(tid, {})[aid] = int(vint)

    print("Saving cache...")
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)

    ore_types = ["Plagioclase", "Kernite", "Arkonor", "Spodumain", "Hedbergite", "Omber"]
    print(f"\n{'='*50}\nSDE Cache Statistics:\n  Types: {len(cache['types'])}\n  Materials: {len(cache.get('materials', {}))}\n  Groups: {len(cache['groups'])}\n  Categories: {len(cache['categories'])}\n  Attributes: {len(cache.get('attributes', {}))}\n{'='*50}")
    print("\nOre material recipes:")
    for name in ore_types:
        tid = cache["type_to_name"].get(name.lower())
        if tid and tid in cache["materials"]:
            print(f"  {name} ({tid}): {cache['materials'][tid]}")
        else:
            print(f"  {name}: No materials data")


if __name__ == "__main__":
    build_cache()
