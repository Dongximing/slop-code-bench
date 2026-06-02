#!/usr/bin/env python3
"""EVE Online Industrial Planner - Parse SDE and emit recipe reports."""

import argparse
import bz2
import csv
import math
import sys
from pathlib import Path
from typing import Dict, List, Optional

import yaml


def _load_csv_bz2(filepath: Path) -> List[Dict]:
    with bz2.open(filepath, 'rt', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def _load_yaml(filepath: Path) -> Dict:
    with open(filepath, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


class SDEParser:
    """Parses SDE files and builds lookup structures."""

    def __init__(self, sde_dir: Path):
        self.sde_dir = sde_dir
        self.types: Dict[int, Dict] = {}
        self.groups: Dict[int, Dict] = {}
        self.categories: Dict[int, Dict] = {}
        self.market_groups: Dict[int, Dict] = {}
        self.meta_types: Dict[int, int] = {}
        self.meta_groups: Dict[int, str] = {}
        self.activities: Dict[int, Dict[int, int]] = {}
        self.products: Dict[int, Dict[int, Dict[int, int]]] = {}
        self.materials: Dict[int, Dict[int, List[Dict]]] = {}
        self.activity_names: Dict[int, str] = {}
        self.ship_volumes: Dict[str, float] = {}
        self.buildable_types: set[int] = set()

        self.load_all()

    def load_all(self):
        self._load_types()
        self._load_groups()
        self._load_categories()
        self._load_market_groups()
        self._load_meta_types()
        self._load_meta_groups()
        self._load_activities()
        self._load_products()
        self._load_materials()
        self._load_activity_names()
        self._load_ship_volumes()
        self._compute_buildable_types()

    def _load_types(self):
        for row in _load_csv_bz2(self.sde_dir / 'invTypes.csv.bz2'):
            if int(row.get('published', 0)) != 1:
                continue
            type_id = int(row['typeID'])
            self.types[type_id] = {
                'typeID': type_id,
                'groupID': int(row['groupID']),
                'typeName': row['typeName'],
                'volume': row.get('volume', '0'),
                'marketGroupID': int(row['marketGroupID']) if row.get('marketGroupID') and row['marketGroupID'] != 'None' else None,
                'mass': row.get('mass', '0'),
                'raceID': row.get('raceID'),
                'basePrice': row.get('basePrice'),
                'description': row.get('description', ''),
            }

    def _load_groups(self):
        for row in _load_csv_bz2(self.sde_dir / 'invGroups.csv.bz2'):
            if int(row.get('published', 0)) != 1:
                continue
            group_id = int(row['groupID'])
            self.groups[group_id] = {
                'groupID': group_id,
                'categoryID': int(row['categoryID']),
                'groupName': row['groupName'],
                'iconID': row.get('iconID'),
            }

    def _load_categories(self):
        for row in _load_csv_bz2(self.sde_dir / 'invCategories.csv.bz2'):
            if int(row.get('published', 0)) != 1:
                continue
            cat_id = int(row['categoryID'])
            self.categories[cat_id] = {
                'categoryID': cat_id,
                'categoryName': row['categoryName'],
                'iconID': row.get('iconID'),
            }

    def _load_market_groups(self):
        for row in _load_csv_bz2(self.sde_dir / 'invMarketGroups.csv.bz2'):
            mg_id = int(row['marketGroupID'])
            parent_id = int(row['parentGroupID']) if row.get('parentGroupID') and row['parentGroupID'] != 'None' else None
            self.market_groups[mg_id] = {
                'marketGroupID': mg_id,
                'parentGroupID': parent_id,
                'marketGroupName': row['marketGroupName'],
                'description': row.get('description', ''),
                'iconID': row.get('iconID'),
                'hasTypes': int(row.get('hasTypes', 0)) == 1,
            }

    def _load_meta_types(self):
        for row in _load_csv_bz2(self.sde_dir / 'invMetaTypes.csv.bz2'):
            if row.get('metaGroupID'):
                self.meta_types[int(row['typeID'])] = int(row['metaGroupID'])

    def _load_meta_groups(self):
        for row in _load_csv_bz2(self.sde_dir / 'invMetaGroups.csv.bz2'):
            self.meta_groups[int(row['metaGroupID'])] = row['metaGroupName']

    def _load_activities(self):
        for row in _load_csv_bz2(self.sde_dir / 'industryActivity.csv.bz2'):
            type_id = int(row['typeID'])
            activity_id = int(row['activityID'])
            self.activities.setdefault(type_id, {})[activity_id] = int(row['time'])

    def _load_products(self):
        for row in _load_csv_bz2(self.sde_dir / 'industryActivityProducts.csv.bz2'):
            type_id = int(row['typeID'])
            activity_id = int(row['activityID'])
            product_type_id = int(row['productTypeID'])
            quantity = int(row['quantity'])
            self.products.setdefault(type_id, {}).setdefault(activity_id, {})[product_type_id] = quantity

    def _load_materials(self):
        for row in _load_csv_bz2(self.sde_dir / 'industryActivityMaterials.csv.bz2'):
            type_id = int(row['typeID'])
            activity_id = int(row['activityID'])
            material = {'typeID': int(row['materialTypeID']), 'quantity': int(row['quantity'])}
            self.materials.setdefault(type_id, {}).setdefault(activity_id, []).append(material)

    def _load_activity_names(self):
        for row in _load_csv_bz2(self.sde_dir / 'ramActivities.csv.bz2'):
            if row.get('published') == '1':
                self.activity_names[int(row['activityID'])] = row['activityName']

    def _load_ship_volumes(self):
        self.ship_volumes = {k: float(v) for k, v in _load_yaml(self.sde_dir / 'ship_volumes.yaml').items()}

    def _compute_buildable_types(self):
        for type_id, activities in self.products.items():
            if 1 in activities or 8 in activities:
                self.buildable_types.add(type_id)

    def get_type_by_name(self, name: str) -> Optional[Dict]:
        for type_data in self.types.values():
            if type_data['typeName'] == name:
                return type_data
        return None

    def get_type_name(self, type_id: int) -> Optional[str]:
        return self.types.get(type_id, {}).get('typeName')

    def get_group_path(self, type_id: int) -> tuple[str, str]:
        type_data = self.types.get(type_id)
        if not type_data:
            return ('Unknown', 'Unknown')
        group_data = self.groups.get(type_data['groupID'])
        if not group_data:
            return ('Unknown', 'Unknown')
        cat_data = self.categories.get(group_data['categoryID'])
        if not cat_data:
            return ('Unknown', group_data['groupName'])
        return (cat_data['categoryName'], group_data['groupName'])

    def get_market_group_path(self, type_id: int) -> Optional[str]:
        type_data = self.types.get(type_id)
        if not type_data or not type_data.get('marketGroupID'):
            return None
        path_parts = []
        mg_id = type_data['marketGroupID']
        while mg_id is not None:
            mg_data = self.market_groups.get(mg_id)
            if not mg_data:
                break
            path_parts.append(mg_data['marketGroupName'])
            mg_id = mg_data.get('parentGroupID')
        return ' > '.join(reversed(path_parts)) if path_parts else None

    def get_tech_level(self, type_id: int) -> str:
        meta_group_id = self.meta_types.get(type_id)
        if not meta_group_id:
            return 'Tech I'
        meta_name = self.meta_groups.get(meta_group_id, '')
        return 'Tech II' if meta_name == 'Tech II' else 'Tech III' if meta_name == 'Tech III' else 'Tech I'

    def get_volume(self, type_id: int) -> float:
        type_data = self.types.get(type_id)
        if not type_data:
            return 0.0
        _, group_name = self.get_group_path(type_id)
        if group_name in self.ship_volumes:
            return self.ship_volumes[group_name]
        try:
            return float(type_data.get('volume') or '0')
        except ValueError:
            return 0.0

    def is_buildable(self, type_id: int) -> bool:
        return type_id in self.buildable_types

    def get_recipe(self, type_id: int) -> Optional[Dict]:
        """Get recipe for manufacturing (1) or reactions (11)."""
        for activity_id in (1, 11):  # Manufacturing, Reactions
            if activity_id not in self.activities.get(type_id, {}):
                continue
            activities = self.activities[type_id]
            products = self.products.get(type_id, {})
            materials = self.materials.get(type_id, {})
            activity_name = self.activity_names.get(activity_id, 'Unknown')

            output_qty = next(iter(products.get(activity_id, {}).values()), 0) if products.get(activity_id) else 0
            run_time_minutes = math.ceil(activities.get(activity_id, 0) / 60)

            mat_list = []
            for mat in materials.get(activity_id, []):
                mat_list.append({'typeID': mat['typeID'], 'quantity': mat['quantity']})

            return {
                'activity': activity_name,
                'output_qty': output_qty,
                'run_time': run_time_minutes,
                'materials': mat_list,
            }
        return None


def _find_product_of_blueprint(sde: SDEParser, blueprint_type_id: int) -> Optional[int]:
    """Return the product typeID of a blueprint, or None."""
    for activity_id, prod_dict in sde.products.get(blueprint_type_id, {}).items():
        if prod_dict:
            return next(iter(prod_dict.keys()))
    return None


def _find_blueprint_producing(sde: SDEParser, product_name: str) -> Optional[int]:
    """Find a blueprint that produces product_name, return its typeID."""
    for bp in sde.types.values():
        if 'Blueprint' not in bp['typeName']:
            continue
        for prod_id in sde.products.get(bp['typeID'], {}).values():
            if sde.get_type_name(next(iter(prod_id), 0)) == product_name:
                return bp['typeID']
    return None


def generate_report(sde: SDEParser, lookup_name: str) -> str:
    """Generate recipe report for a product or blueprint."""

    # Try direct lookup first
    type_data = sde.get_type_by_name(lookup_name)

    # If not found, try to find a blueprint that produces an item with this name
    if not type_data:
        bp_id = _find_blueprint_producing(sde, lookup_name)
        if bp_id:
            type_data = sde.types[bp_id]

    if not type_data:
        return f"Error: Item '{lookup_name}' not found or not published.\n"

    type_id = type_data['typeID']
    display_type_id = type_id
    display_type_data = type_data

    # If the lookup was for a blueprint (name contains "Blueprint"),
    # show the product info instead
    if 'Blueprint' in type_data['typeName']:
        product_id = _find_product_of_blueprint(sde, type_id)
        if product_id:
            product_data = sde.types.get(product_id)
            if product_data:
                display_type_id = product_id
                display_type_data = product_data

    # Get recipe: use blueprint if available, otherwise direct lookup
    recipe = None
    if display_type_id != type_id:
        recipe = sde.get_recipe(type_id)
    if not recipe:
        recipe = sde.get_recipe(display_type_id)

    if not recipe:
        return f"Error: No industry activity found for '{lookup_name}'.\n"

    # Build report
    cat_name, group_name = sde.get_group_path(display_type_id)
    mg_path = sde.get_market_group_path(display_type_id)
    tech_level = sde.get_tech_level(display_type_id)
    volume = sde.get_volume(display_type_id)

    lines = [
        f"ITEM: {display_type_data['typeName']} ({display_type_id})",
        f"Group: {cat_name} > {group_name}",
        f"Market Group: {mg_path if mg_path else 'None'}",
        f"Tech Level: {tech_level}",
        f"Volume: {volume:.2f}",
        "",
        "Recipe:",
        f"Activity: {recipe['activity']}",
        f"Output Quantity: {recipe['output_qty']}",
        f"Run Time: {recipe['run_time']}",
        "| Item | Quantity | Buildable |",
        "|:-:|:---:|---:|",
    ]

    # Sort materials alphabetically (case-insensitive)
    sorted_materials = sorted(
        recipe['materials'],
        key=lambda m: (sde.get_type_name(m['typeID']) or '').lower()
    )
    for mat in sorted_materials:
        mat_name = sde.get_type_name(mat['typeID']) or 'Unknown'
        buildable = 'Yes' if sde.is_buildable(mat['typeID']) else 'No'
        lines.append(f"| {mat_name} | {mat['quantity']} | {buildable} |")

    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(
        description='EVE Online Industrial Planner - Generate recipe reports from SDE'
    )
    parser.add_argument('command', choices=['recipe'])
    parser.add_argument('target', help='Product or Blueprint name (exact match, case-sensitive)')
    parser.add_argument('--sde', required=True, help='Path to SDE directory')

    args = parser.parse_args()

    sde_path = Path(args.sde)
    if not sde_path.exists():
        print(f"Error: SDE directory '{args.sde}' does not exist.", file=sys.stderr)
        sys.exit(1)

    sde = SDEParser(sde_path)

    if args.command == 'recipe':
        print(generate_report(sde, args.target), end='')


if __name__ == '__main__':
    main()
