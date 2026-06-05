#!/usr/bin/env python3
"""
EVE Online Industry Recipe Planner

Parses the SDE and emits a deterministic recipe report for a target product or blueprint.
"""

import argparse
import bz2
import csv
import math
import os
import sys
from collections import defaultdict
from typing import Dict, List, Optional, Tuple, Any


class SDELoader:
    """Loads and parses EVE Online SDE data."""

    def __init__(self, sde_path: str):
        self.sde_path = sde_path
        self.types: Dict[int, Dict] = {}  # typeID -> type info
        self.groups: Dict[int, Dict] = {}  # groupID -> group info
        self.categories: Dict[int, Dict] = {}  # categoryID -> category info
        self.market_groups: Dict[int, Dict] = {}  # marketGroupID -> market group info
        self.meta_types: Dict[int, int] = {}  # typeID -> metaGroupID
        self.meta_groups: Dict[int, str] = {}  # metaGroupID -> metaGroupName
        self.activities: Dict[int, str] = {}  # activityID -> activityName
        self.ship_volumes: Dict[str, float] = {}  # groupName -> packaged volume

        # Industry data - keyed by blueprint typeID
        self.blueprint_products: Dict[int, List[Dict]] = defaultdict(list)  # bpTypeID -> list of products
        self.blueprint_materials: Dict[int, List[Dict]] = defaultdict(list)  # bpTypeID -> list of materials
        self.blueprint_times: Dict[int, Dict[int, int]] = defaultdict(dict)  # bpTypeID -> {activityID: time}

        # Buildable items (items that can be produced via industry)
        self.buildable_items: set = set()

        self._load_all()

    def _load_all(self):
        """Load all SDE data."""
        self._load_types()
        self._load_groups()
        self._load_categories()
        self._load_market_groups()
        self._load_meta_types()
        self._load_meta_groups()
        self._load_activities()
        self._load_industry_products()
        self._load_industry_materials()
        self._load_industry_activities()
        self._load_ship_volumes()
        self._compute_buildable_items()

    def _read_csv_bz2(self, filename: str) -> List[Dict]:
        """Read a bz2 compressed CSV file."""
        filepath = os.path.join(self.sde_path, filename)
        with bz2.open(filepath, 'rt', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            return list(reader)

    def _load_types(self):
        """Load invTypes."""
        for row in self._read_csv_bz2('invTypes.csv.bz2'):
            type_id = int(row['typeID'])
            published_val = row['published'].lower() in ('true', '1')

            # Parse volume - handle various formats
            volume_str = row['volume']
            try:
                if volume_str and volume_str != 'None' and volume_str != '0E-10':
                    volume = float(volume_str)
                else:
                    volume = 0.0
            except ValueError:
                volume = 0.0

            self.types[type_id] = {
                'typeID': type_id,
                'groupID': int(row['groupID']),
                'typeName': row['typeName'],
                'description': row.get('description', ''),
                'mass': row.get('mass', '0'),
                'volume': volume,
                'capacity': row.get('capacity', '0'),
                'portionSize': int(row['portionSize']) if row['portionSize'] else 1,
                'raceID': row.get('raceID'),
                'basePrice': row.get('basePrice'),
                'published': published_val,
                'marketGroupID': int(row['marketGroupID']) if row['marketGroupID'] and row['marketGroupID'] != 'None' else None,
                'iconID': row.get('iconID'),
                'soundID': row.get('soundID'),
                'graphicID': row.get('graphicID'),
            }

    def _load_groups(self):
        """Load invGroups."""
        for row in self._read_csv_bz2('invGroups.csv.bz2'):
            group_id = int(row['groupID'])
            self.groups[group_id] = {
                'groupID': group_id,
                'categoryID': int(row['categoryID']),
                'groupName': row['groupName'],
            }

    def _load_categories(self):
        """Load invCategories."""
        for row in self._read_csv_bz2('invCategories.csv.bz2'):
            category_id = int(row['categoryID'])
            self.categories[category_id] = {
                'categoryID': category_id,
                'categoryName': row['categoryName'],
            }

    def _load_market_groups(self):
        """Load invMarketGroups."""
        for row in self._read_csv_bz2('invMarketGroups.csv.bz2'):
            group_id = int(row['marketGroupID'])
            parent_id = int(row['parentGroupID']) if row['parentGroupID'] and row['parentGroupID'] != 'None' else None
            self.market_groups[group_id] = {
                'marketGroupID': group_id,
                'parentGroupID': parent_id,
                'marketGroupName': row['marketGroupName'],
            }

    def _load_meta_types(self):
        """Load invMetaTypes."""
        for row in self._read_csv_bz2('invMetaTypes.csv.bz2'):
            type_id = int(row['typeID'])
            meta_group_id = int(row['metaGroupID']) if row['metaGroupID'] and row['metaGroupID'] != 'None' else None
            if meta_group_id:
                self.meta_types[type_id] = meta_group_id

    def _load_meta_groups(self):
        """Load invMetaGroups."""
        for row in self._read_csv_bz2('invMetaGroups.csv.bz2'):
            group_id = int(row['metaGroupID'])
            self.meta_groups[group_id] = row['metaGroupName']

    def _load_activities(self):
        """Load ramActivities."""
        for row in self._read_csv_bz2('ramActivities.csv.bz2'):
            activity_id = int(row['activityID'])
            self.activities[activity_id] = row['activityName']

    def _load_industry_products(self):
        """Load industryActivityProducts."""
        for row in self._read_csv_bz2('industryActivityProducts.csv.bz2'):
            bp_type_id = int(row['typeID'])
            activity_id = int(row['activityID'])
            product_type_id = int(row['productTypeID'])
            quantity = int(row['quantity'])
            self.blueprint_products[bp_type_id].append({
                'activityID': activity_id,
                'productTypeID': product_type_id,
                'quantity': quantity,
            })

    def _load_industry_materials(self):
        """Load industryActivityMaterials."""
        for row in self._read_csv_bz2('industryActivityMaterials.csv.bz2'):
            bp_type_id = int(row['typeID'])
            activity_id = int(row['activityID'])
            material_type_id = int(row['materialTypeID'])
            quantity = int(row['quantity'])
            self.blueprint_materials[bp_type_id].append({
                'activityID': activity_id,
                'materialTypeID': material_type_id,
                'quantity': quantity,
            })

    def _load_industry_activities(self):
        """Load industryActivity for times."""
        for row in self._read_csv_bz2('industryActivity.csv.bz2'):
            bp_type_id = int(row['typeID'])
            activity_id = int(row['activityID'])
            time_val = int(row['time'])
            self.blueprint_times[bp_type_id][activity_id] = time_val

    def _load_ship_volumes(self):
        """Load ship_volumes.yaml."""
        import yaml

        filepath = os.path.join(self.sde_path, 'ship_volumes.yaml')
        with open(filepath, 'r') as f:
            data = yaml.safe_load(f)
            if data:
                self.ship_volumes = {k: float(v) for k, v in data.items()}

    def _compute_buildable_items(self):
        """Compute the set of items that can be produced via industry."""
        # An item is buildable if it appears as a product in industryActivityProducts
        for bp_type_id, products in self.blueprint_products.items():
            for product in products:
                # Manufacturing (activity 1) or Reactions (activity 11)
                if product['activityID'] in (1, 11):
                    self.buildable_items.add(product['productTypeID'])

    def find_type_by_name(self, name: str) -> Optional[Dict]:
        """Find a type by its exact typeName (case-sensitive)."""
        for type_info in self.types.values():
            if type_info['typeName'] == name and type_info['published']:
                return type_info
        return None

    def find_blueprint_for_product(self, product_type_id: int) -> Optional[int]:
        """Find the blueprint typeID that produces a given product."""
        for bp_type_id, products in self.blueprint_products.items():
            for product in products:
                if product['productTypeID'] == product_type_id:
                    # Only return for Manufacturing or Reactions
                    if product['activityID'] in (1, 11):
                        return bp_type_id
        return None

    def is_blueprint(self, type_info: Dict) -> bool:
        """Check if a type is a blueprint."""
        group_info = self.groups.get(type_info['groupID'])
        return group_info is not None and group_info['categoryID'] == 9

    def get_market_group_path(self, market_group_id: Optional[int]) -> List[str]:
        """Get the full market group path from root to leaf."""
        if market_group_id is None:
            return []

        path = []
        current_id = market_group_id
        while current_id is not None:
            group_info = self.market_groups.get(current_id)
            if group_info is None:
                break
            path.append(group_info['marketGroupName'])
            current_id = group_info['parentGroupID']

        return list(reversed(path))

    def get_tech_level(self, type_id: int) -> str:
        """Get the tech level for an item."""
        meta_group_id = self.meta_types.get(type_id)
        if meta_group_id is None:
            return "Tech I"

        meta_group_name = self.meta_groups.get(meta_group_id, "")
        if meta_group_name == "Tech II":
            return "Tech II"
        if meta_group_name == "Tech III":
            return "Tech III"
        return "Tech I"

    def get_volume(self, type_info: Dict) -> float:
        """Get the volume for an item, using packaged volume for ships."""
        group_info = self.groups.get(type_info['groupID'])

        if group_info:
            category_info = self.categories.get(group_info['categoryID'])

            # Check if this is a ship (category 6)
            if category_info and category_info['categoryName'] == 'Ship':
                packaged_volume = self.ship_volumes.get(group_info['groupName'])
                if packaged_volume is not None:
                    return packaged_volume

        return type_info['volume']

    def is_buildable(self, type_id: int) -> bool:
        """Check if an item can be produced via industry."""
        return type_id in self.buildable_items

    def get_recipe(self, blueprint_type_id: int, activity_id: int) -> Tuple[List[Dict], int, int]:
        """Get recipe for a blueprint and activity.

        Returns: (materials, output_quantity, run_time_minutes)
        """
        materials = []
        for mat in self.blueprint_materials.get(blueprint_type_id, []):
            if mat['activityID'] == activity_id:
                type_info = self.types.get(mat['materialTypeID'])
                if type_info:
                    materials.append({
                        'typeID': mat['materialTypeID'],
                        'typeName': type_info['typeName'],
                        'quantity': mat['quantity'],
                        'buildable': self.is_buildable(mat['materialTypeID']),
                    })

        output_quantity = 0
        for product in self.blueprint_products.get(blueprint_type_id, []):
            if product['activityID'] == activity_id:
                output_quantity = product['quantity']
                break

        time_seconds = self.blueprint_times.get(blueprint_type_id, {}).get(activity_id, 0)
        run_time_minutes = math.ceil(time_seconds / 60)

        return materials, output_quantity, run_time_minutes


def format_materials_table(materials: List[Dict]) -> str:
    """Format the materials table."""
    # Sort alphabetically by item name (case-insensitive)
    # For ties in case-insensitive comparison, use case-sensitive as tiebreaker
    sorted_materials = sorted(materials, key=lambda m: (m['typeName'].lower(), m['typeName']))

    lines = [
        "| Item | Quantity | Buildable |",
        "|:-:|:---:|---:|",
    ]

    for mat in sorted_materials:
        buildable_str = "Yes" if mat['buildable'] else "No"
        lines.append(f"| {mat['typeName']} | {mat['quantity']} | {buildable_str} |")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description='EVE Online Industry Recipe Planner')
    parser.add_argument('command', choices=['recipe'], help='Command to execute')
    parser.add_argument('name', help='Product or Blueprint name (exact, case-sensitive)')
    parser.add_argument('--sde', required=True, help='Path to SDE directory')

    args = parser.parse_args()

    # Load SDE
    sde = SDELoader(args.sde)

    type_info = sde.find_type_by_name(args.name)
    if type_info is None:
        print(f"Error: Item '{args.name}' not found", file=sys.stderr)
        sys.exit(1)

    type_id = type_info['typeID']

    # Determine if this is a blueprint or product
    if sde.is_blueprint(type_info):
        blueprint_type_id = type_id

        products = sde.blueprint_products.get(blueprint_type_id, [])
        product_info = None
        activity_id = None

        for activity in [1, 11]:
            for product in products:
                if product['activityID'] == activity:
                    product_type_id = product['productTypeID']
                    product_info = sde.types.get(product_type_id)
                    activity_id = activity
                    break
            if product_info:
                break

        if product_info is None:
            print(f"Error: No product found for blueprint '{args.name}'", file=sys.stderr)
            sys.exit(1)

        product_type_id = product_info['typeID']
    else:
        blueprint_type_id = sde.find_blueprint_for_product(type_id)
        if blueprint_type_id is None:
            print(f"Error: No blueprint found for product '{args.name}'", file=sys.stderr)
            sys.exit(1)

        blueprint_info = sde.types.get(blueprint_type_id)
        product_info = type_info

        products = sde.blueprint_products.get(blueprint_type_id, [])
        activity_id = None
        for product in products:
            if product['productTypeID'] == type_id:
                activity_id = product['activityID']
                break

        if activity_id is None:
            print(f"Error: Could not determine activity for '{args.name}'", file=sys.stderr)
            sys.exit(1)

    activity_name = sde.activities.get(activity_id, "Unknown")

    # Get recipe
    materials, output_quantity, run_time = sde.get_recipe(blueprint_type_id, activity_id)

    product_type_id = product_info['typeID']
    product_name = product_info['typeName']

    group_info = sde.groups.get(product_info['groupID'])
    category_id = group_info['categoryID'] if group_info else None
    category_info = sde.categories.get(category_id) if category_id else None

    category_name = category_info['categoryName'] if category_info else "Unknown"
    group_name = group_info['groupName'] if group_info else "Unknown"

    market_group_id = product_info.get('marketGroupID')
    market_group_path = sde.get_market_group_path(market_group_id)
    market_group_str = " > ".join(market_group_path) if market_group_path else "None"

    tech_level = sde.get_tech_level(product_type_id)

    volume = sde.get_volume(product_info)

    # Format volume - examples show 15000.00 for integers, 0.025 for decimals
    if volume == int(volume):
        volume_str = f"{volume:.2f}"
    elif volume >= 1:
        volume_str = f"{volume:.2f}".rstrip('0').rstrip('.')
    else:
        volume_str = f"{volume:.3f}".rstrip('0').rstrip('.')

    # Output the canonical block
    print(f"ITEM: {product_name} ({product_type_id})")
    print(f"Group: {category_name} > {group_name}")
    print(f"Market Group: {market_group_str}")
    print(f"Tech Level: {tech_level}")
    print(f"Volume: {volume_str}")
    print()
    print("Recipe:")
    print(f"Activity: {activity_name}")
    print(f"Output Quantity: {output_quantity}")
    print(f"Run Time: {run_time}")
    print(format_materials_table(materials))


if __name__ == '__main__':
    main()
