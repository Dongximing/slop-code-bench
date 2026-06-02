#!/usr/bin/env python3
"""Build an in-memory cache of SDE reprocessing data for faster lookups."""
import bz2
import csv
import json
from typing import Any

SDE_PATH = "/workspace/sde"
CACHE_FILE = "/workspace/sde_cache.json"


def load_sde_data() -> dict[str, Any]:
    cache = {"type_names": {}, "type_info": {}, "type_materials": {}, "group_hierarchy": {}, "ref_skill_map": {}}

    def _read_int(row: dict, key: str) -> int | None:
        try:
            return int(row[key])
        except (ValueError, KeyError):
            return None

    def _read_float(row: dict, key: str) -> str | None:
        return row.get(key)

    print("Loading invTypes...")
    with bz2.open(f"{SDE_PATH}/invTypes.csv.bz2", "rt") as f:
        for row in csv.DictReader(f):
            if tid := _read_int(row, "typeID"):
                name = row["typeName"]
                gid = _read_int(row, "groupID") or 0
                volume = row.get("volume", "")
                cache["type_names"][name] = tid
                cache["type_info"][tid] = {"group_id": gid, "volume": volume, "portionSize": row.get("portionSize", "")}
                if mass := row.get("mass"):
                    cache["type_info"][tid]["mass"] = mass

    print("Loading invTypeMaterials...")
    with bz2.open(f"{SDE_PATH}/invTypeMaterials.csv.bz2", "rt") as f:
        for row in csv.DictReader(f):
            if tid := _read_int(row, "typeID"):
                mat_tid = _read_int(row, "materialTypeID")
                qty = _read_int(row, "quantity")
                if mat_tid and qty:
                    cache.setdefault("type_materials", {}).setdefault(tid, []).append((mat_tid, qty))

    print("Loading invGroups...")
    with bz2.open(f"{SDE_PATH}/invGroups.csv.bz2", "rt") as f:
        for row in csv.DictReader(f):
            if gid := _read_int(row, "groupID"):
                cache["group_hierarchy"][gid] = {"category_id": _read_int(row, "categoryID") or 0, "name": row.get("groupName", "")}

    print("Loading dgmTypeAttributes for refSkill mapping...")
    with bz2.open(f"{SDE_PATH}/dgmTypeAttributes.csv.bz2", "rt") as f:
        for row in csv.DictReader(f):
            if tid := _read_int(row, "typeID"):
                aid = _read_int(row, "attributeID")
                if aid == 1288 and (vfloat := row.get("valueFloat")):
                    cache["ref_skill_map"][tid] = int(float(vfloat))

    print("Saving cache...")
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)

    print(f"Cache saved to {CACHE_FILE}\nType names: {len(cache['type_names'])}\nType info: {len(cache['type_info'])}\nType materials: {len(cache['type_materials'])}")
    return cache


if __name__ == "__main__":
    load_sde_data()
