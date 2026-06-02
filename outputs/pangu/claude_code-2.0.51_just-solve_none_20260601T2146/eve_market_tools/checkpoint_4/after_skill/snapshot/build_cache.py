#!/usr/bin/env python3
"""Build a properly formatted SDE cache for the reprocessing API."""
import bz2
import csv
import gzip
import json
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SDE_PATH = os.path.join(SCRIPT_DIR, "sde")
CACHE_FILE = os.path.join(SCRIPT_DIR, "sde_cache.json")


def open_csv(filepath):
    try:
        return bz2.open(filepath, "rt", encoding="utf-8")
    except Exception:
        try:
            return gzip.open(filepath, "rt", encoding="utf-8")
        except Exception:
            try:
                return open(filepath, "r", encoding="utf-8")
            except Exception:
                return None


def build_cache():
    cache = {"types": {}, "materials": {}, "groups": {}, "categories": {}}

    def load_csv(filepath):
        f = open_csv(filepath)
        if f:
            return csv.DictReader(f)
        return []

    for row in load_csv(f"{SDE_PATH}/invTypes.csv.bz2"):
        try:
            tid = str(int(row["typeID"]))
            cache["types"][tid] = {
                "name": row["typeName"],
                "group_id": int(row["groupID"]),
                "volume": float(row["volume"]),
                "portion_size": int(row["portionSize"]),
            }
        except (ValueError, KeyError):
            pass

    for row in load_csv(f"{SDE_PATH}/invTypeMaterials.csv.bz2"):
        try:
            tid = str(int(row["typeID"]))
            mat_tid = str(int(row["materialTypeID"]))
            qty = int(row["quantity"])
            cache["materials"].setdefault(tid, []).append((mat_tid, qty))
        except (ValueError, KeyError):
            pass

    for row in load_csv(f"{SDE_PATH}/invGroups.csv.bz2"):
        try:
            gid = str(int(row["groupID"]))
            cache["groups"][gid] = {
                "name": row.get("groupName", ""),
                "category_id": int(row["categoryID"]),
            }
        except (ValueError, KeyError):
            pass

    for row in load_csv(f"{SDE_PATH}/invCategories.csv.bz2"):
        try:
            cid = str(int(row["categoryID"]))
            cache["categories"][cid] = row["categoryName"]
        except (ValueError, KeyError):
            pass

    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)

    print(f"Cache saved! Stats: Types: {len(cache['types'])}, Materials: {len(cache.get('materials', {}))}, Groups: {len(cache['groups'])}")


if __name__ == "__main__":
    build_cache()
