#!/usr/bin/env python3
"""
EVE Online Industry Recipe Planner

A command-line tool that helps EVE Online industrialists plan builds from
the official Static Data Export (SDE). Parses the SDE and emits a deterministic
recipe report for a target product or blueprint.
"""

import argparse
import bz2
import csv
import math
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Set

import yaml


def load_bz2_csv(filepath: str) -> List[Dict[str, str]]:
    """Load a bz2-compressed CSV file and return a list of row dictionaries."""
    rows = []
    with bz2.open(filepath, 'rt', encoding='utf-8', newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def load_yaml(filepath: str) -> Dict:
    """Load a YAML file."""
    with open(filepath, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


class SDE:
    """Static Data Export handler for EVE Online."""

    def __init__(self, sde_dir: str):
        self.sde_dir = Path(sde_dir)
        self._types: Optional[Dict[int, Dict]] = None
        self._groups: Optional[Dict[int, Dict]] = None
        self._categories: Optional[Dict[int, Dict]] = None
        self._market_groups: Optional[Dict[int, Dict]] = None
        self._meta_types: Optional[Dict[int, Dict]] = None
        self._meta_groups: Optional[Dict[int, Dict]] = None
        self._activities: Optional[Dict[int, Dict]] = None
        self._activity_products: Optional[Dict[int, List[Dict]]] = None
        self._activity_materials: Optional[Dict[int, List[Dict]]] = None
        self._activity_times: Optional[Dict[Tuple[int, int], int]] = None
        self._ship_volumes: Optional[Dict[str, float]] = None
        self._buildable_types: Optional[Set[int]] = None

    def _load_types(self):
        """Load invTypes.csv.bz2"""
        if self._types is not None:
            return
        self._types = {}
        rows = load_bz2_csv(self.sde_dir / 'invTypes.csv.bz2')
        for row in rows:
            type_id = int(row['typeID'])
            published = row.get('published', '0')
            self._types[type_id] = {
                'typeID': type_id,
                'groupID': int(row['groupID']) if row['groupID'] and row['groupID'] != 'None' else None,
                'typeName': row['typeName'],
                'volume': float(row['volume']) if row['volume'] and row['volume'] != 'None' else 0.0,
                'published': int(published) if published and published != 'None' else 0,
                'marketGroupID': int(row['marketGroupID']) if row['marketGroupID'] and row['marketGroupID'] != 'None' else None,
            }

    def _load_groups(self):
        """Load invGroups.csv.bz2"""
        if self._groups is not None:
            return
        self._groups = {}
        rows = load_bz2_csv(self.sde_dir / 'invGroups.csv.bz2')
        for row in rows:
            group_id = int(row['groupID'])
            self._groups[group_id] = {
                'groupID': group_id,
                'categoryID': int(row['categoryID']) if row['categoryID'] and row['categoryID'] != 'None' else None,
                'groupName': row['groupName'],
                'published': int(row['published']) if row['published'] and row['published'] != 'None' else 0,
            }

    def _load_categories(self):
        """Load invCategories.csv.bz2"""
        if self._categories is not None:
            return
        self._categories = {}
        rows = load_bz2_csv(self.sde_dir / 'invCategories.csv.bz2')
        for row in rows:
            cat_id = int(row['categoryID'])
            self._categories[cat_id] = {
                'categoryID': cat_id,
                'categoryName': row['categoryName'],
                'published': int(row['published']) if row['published'] and row['published'] != 'None' else 0,
            }

    def _load_market_groups(self):
        """Load invMarketGroups.csv.bz2"""
        if self._market_groups is not None:
            return
        self._market_groups = {}
        rows = load_bz2_csv(self.sde_dir / 'invMarketGroups.csv.bz2')
        for row in rows:
            mg_id = int(row['marketGroupID'])
            parent_id = int(row['parentGroupID']) if row['parentGroupID'] and row['parentGroupID'] != 'None' else None
            self._market_groups[mg_id] = {
                'marketGroupID': mg_id,
                'parentGroupID': parent_id,
                'marketGroupName': row['marketGroupName'],
            }

    def _load_meta_types(self):
        """Load invMetaTypes.csv.bz2"""
        if self._meta_types is not None:
            return
        self._meta_types = {}
        rows = load_bz2_csv(self.sde_dir / 'invMetaTypes.csv.bz2')
        for row in rows:
            type_id = int(row['typeID'])
            self._meta_types[type_id] = {
                'typeID': type_id,
                'parentTypeID': int(row['parentTypeID']) if row['parentTypeID'] and row['parentTypeID'] != 'None' else None,
                'metaGroupID': int(row['metaGroupID']) if row['metaGroupID'] and row['metaGroupID'] != 'None' else None,
            }

    def _load_meta_groups(self):
        """Load invMetaGroups.csv.bz2"""
        if self._meta_groups is not None:
            return
        self._meta_groups = {}
        rows = load_bz2_csv(self.sde_dir / 'invMetaGroups.csv.bz2')
        for row in rows:
            mg_id = int(row['metaGroupID'])
            self._meta_groups[mg_id] = {
                'metaGroupID': mg_id,
                'metaGroupName': row['metaGroupName'],
            }

    def _load_activities(self):
        """Load ramActivities.csv.bz2"""
        if self._activities is not None:
            return
        self._activities = {}
        rows = load_bz2_csv(self.sde_dir / 'ramActivities.csv.bz2')
        for row in rows:
            act_id = int(row['activityID'])
            self._activities[act_id] = {
                'activityID': act_id,
                'activityName': row['activityName'],
            }

    def _load_industry_activity(self):
        """Load industryActivity.csv.bz2 (blueprint activity times)"""
        if self._activity_times is not None:
            return
        self._activity_times = {}
        rows = load_bz2_csv(self.sde_dir / 'industryActivity.csv.bz2')
        for row in rows:
            bp_id = int(row['typeID'])
            act_id = int(row['activityID'])
            time = int(row['time'])
            self._activity_times[(bp_id, act_id)] = time

    def _load_industry_products(self):
        """Load industryActivityProducts.csv.bz2"""
        if self._activity_products is not None:
            return
        self._activity_products = {}
        rows = load_bz2_csv(self.sde_dir / 'industryActivityProducts.csv.bz2')
        for row in rows:
            bp_id = int(row['typeID'])
            if bp_id not in self._activity_products:
                self._activity_products[bp_id] = []
            self._activity_products[bp_id].append({
                'activityID': int(row['activityID']),
                'productTypeID': int(row['productTypeID']),
                'quantity': int(row['quantity']),
            })

    def _load_industry_materials(self):
        """Load industryActivityMaterials.csv.bz2"""
        if self._activity_materials is not None:
            return
        self._activity_materials = {}
        rows = load_bz2_csv(self.sde_dir / 'industryActivityMaterials.csv.bz2')
        for row in rows:
            bp_id = int(row['typeID'])
            if bp_id not in self._activity_materials:
                self._activity_materials[bp_id] = []
            self._activity_materials[bp_id].append({
                'activityID': int(row['activityID']),
                'materialTypeID': int(row['materialTypeID']),
                'quantity': int(row['quantity']),
            })

    def _load_ship_volumes(self):
        """Load ship_volumes.yaml"""
        if self._ship_volumes is not None:
            return
        self._ship_volumes = load_yaml(self.sde_dir / 'ship_volumes.yaml') or {}

    def _compute_buildable_types(self):
        """Compute which types can be built via industry."""
        if self._buildable_types is not None:
            return
        self._buildable_types = set()
        self._load_industry_products()
        for bp_id, products in self._activity_products.items():
            for prod in products:
                # Only Manufacturing (1) and Reactions (11) are buildable
                if prod['activityID'] in (1, 11):
                    self._buildable_types.add(prod['productTypeID'])

    def get_type_by_name(self, name: str) -> Optional[Dict]:
        """Get a type by its exact typeName."""
        self._load_types()
        for type_data in self._types.values():
            if type_data['typeName'] == name and type_data['published'] == 1:
                return type_data
        return None

    def get_type(self, type_id: int) -> Optional[Dict]:
        """Get a type by its typeID."""
        self._load_types()
        return self._types.get(type_id)

    def get_group(self, group_id: int) -> Optional[Dict]:
        """Get a group by its groupID."""
        self._load_groups()
        return self._groups.get(group_id)

    def get_category(self, category_id: int) -> Optional[Dict]:
        """Get a category by its categoryID."""
        self._load_categories()
        return self._categories.get(category_id)

    def get_market_group_path(self, market_group_id: Optional[int]) -> Optional[str]:
        """Get the full market group path from root to leaf."""
        if market_group_id is None:
            return None
        self._load_market_groups()
        path = []
        current_id = market_group_id
        visited = set()
        while current_id is not None and current_id not in visited:
            visited.add(current_id)
            mg = self._market_groups.get(current_id)
            if mg is None:
                break
            path.append(mg['marketGroupName'])
            current_id = mg['parentGroupID']
        return ' > '.join(reversed(path)) if path else None

    def get_tech_level(self, type_id: int) -> str:
        """Get the tech level for a type."""
        self._load_meta_types()
        self._load_meta_groups()
        meta_type = self._meta_types.get(type_id)
        if meta_type is None:
            return 'Tech I'
        meta_group_id = meta_type.get('metaGroupID')
        if meta_group_id is None:
            return 'Tech I'
        meta_group = self._meta_groups.get(meta_group_id)
        if meta_group is None:
            return 'Tech I'
        meta_name = meta_group.get('metaGroupName', '')
        if meta_name == 'Tech I':
            return 'Tech I'
        elif meta_name == 'Tech II':
            return 'Tech II'
        elif meta_name == 'Tech III':
            return 'Tech III'
        return 'Tech I'

    def get_packaged_volume(self, type_data: Dict) -> float:
        """Get the packaged volume for a type."""
        self._load_groups()
        self._load_categories()

        group_id = type_data.get('groupID')
        if group_id is None:
            return type_data.get('volume', 0.0)

        group = self.get_group(group_id)
        if group is None:
            return type_data.get('volume', 0.0)

        category_id = group.get('categoryID')
        if category_id is None:
            return type_data.get('volume', 0.0)

        category = self.get_category(category_id)
        if category is None or category.get('categoryName') != 'Ship':
            return type_data.get('volume', 0.0)

        # It's a ship - use packaged volume
        self._load_ship_volumes()
        group_name = group.get('groupName', '')
        packaged_volume = self._ship_volumes.get(group_name)
        if packaged_volume is not None:
            return packaged_volume

        return type_data.get('volume', 0.0)

    def is_buildable(self, type_id: int) -> bool:
        """Check if a type can be built via industry."""
        self._compute_buildable_types()
        return type_id in self._buildable_types

    def find_blueprint_for_product(self, product_type_id: int) -> Optional[Tuple[int, int]]:
        """Find the blueprint and activity that produces a product."""
        self._load_industry_products()
        for bp_id, products in self._activity_products.items():
            for prod in products:
                if prod['productTypeID'] == product_type_id:
                    # Return first manufacturing or reactions activity
                    if prod['activityID'] in (1, 11):
                        return (bp_id, prod['activityID'])
        return None

    def get_blueprint_recipe(self, bp_id: int, activity_id: int) -> Optional[Dict]:
        """Get the recipe for a blueprint activity."""
        self._load_industry_products()
        self._load_industry_materials()
        self._load_industry_activity()

        # Get product
        products = self._activity_products.get(bp_id, [])
        product_info = None
        for prod in products:
            if prod['activityID'] == activity_id:
                product_info = prod
                break

        if product_info is None:
            return None

        # Get materials
        materials = []
        for mat in self._activity_materials.get(bp_id, []):
            if mat['activityID'] == activity_id:
                materials.append(mat)

        # Get time
        time_seconds = self._activity_times.get((bp_id, activity_id), 0)
        time_minutes = math.ceil(time_seconds / 60)

        return {
            'productTypeID': product_info['productTypeID'],
            'quantity': product_info['quantity'],
            'materials': materials,
            'time_minutes': time_minutes,
            'activityID': activity_id,
        }

    def get_activity_name(self, activity_id: int) -> str:
        """Get the activity name for an activity ID."""
        self._load_activities()
        act = self._activities.get(activity_id)
        if act is None:
            return 'Unknown'
        return act['activityName']


def format_recipe(sde: SDE, type_data: Dict) -> str:
    """Format the recipe output for a type."""
    type_id = type_data['typeID']
    type_name = type_data['typeName']

    # Get group and category info
    group_id = type_data.get('groupID')
    group = sde.get_group(group_id) if group_id else None
    category_id = group.get('categoryID') if group else None
    category = sde.get_category(category_id) if category_id else None

    category_name = category['categoryName'] if category else 'Unknown'
    group_name = group['groupName'] if group else 'Unknown'

    # Get market group path
    market_group_id = type_data.get('marketGroupID')
    market_group_path = sde.get_market_group_path(market_group_id)

    # Get tech level
    tech_level = sde.get_tech_level(type_id)

    # Get packaged volume
    volume = sde.get_packaged_volume(type_data)

    # Find blueprint for this product
    recipe_info = None
    bp_id = None
    activity_id = None

    result = sde.find_blueprint_for_product(type_id)
    if result:
        bp_id, activity_id = result
        recipe_info = sde.get_blueprint_recipe(bp_id, activity_id)

    if recipe_info is None:
        raise ValueError(f"No recipe found for {type_name}")

    activity_name = sde.get_activity_name(activity_id)

    # Build output
    lines = []

    # ITEM line
    lines.append(f"ITEM: {type_name} ({type_id})")

    # Group line
    lines.append(f"Group: {category_name} > {group_name}")

    # Market Group line
    lines.append(f"Market Group: {market_group_path if market_group_path else 'None'}")

    # Tech Level line
    lines.append(f"Tech Level: {tech_level}")

    # Volume line - show with appropriate decimal places
    # If volume is an integer, show with 2 decimal places (e.g., 15000.00)
    # Otherwise show minimal decimal places needed (e.g., 0.01, 0.025)
    if volume == int(volume):
        lines.append(f"Volume: {volume:.2f}")
    else:
        # Format with minimal decimal places
        formatted = f"{volume:.10f}".rstrip('0').rstrip('.')
        lines.append(f"Volume: {formatted}")

    # Empty line before recipe
    lines.append("")

    # Recipe header
    lines.append("Recipe:")

    # Activity line
    lines.append(f"Activity: {activity_name}")

    # Output Quantity
    lines.append(f"Output Quantity: {recipe_info['quantity']}")

    # Run Time
    lines.append(f"Run Time: {recipe_info['time_minutes']}")

    # Materials table
    lines.append("| Item | Quantity | Buildable |")
    lines.append("|:-:|:---:|---:|")

    # Sort materials alphabetically by item name (case-insensitive)
    materials = recipe_info['materials']
    material_info = []
    for mat in materials:
        mat_type = sde.get_type(mat['materialTypeID'])
        if mat_type:
            material_info.append({
                'name': mat_type['typeName'],
                'quantity': mat['quantity'],
                'buildable': sde.is_buildable(mat['materialTypeID']),
            })

    material_info.sort(key=lambda x: x['name'].lower())

    for mat in material_info:
        buildable_str = 'Yes' if mat['buildable'] else 'No'
        lines.append(f"| {mat['name']} | {mat['quantity']} | {buildable_str} |")

    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(
        description='EVE Online Industry Recipe Planner'
    )
    parser.add_argument(
        'command',
        choices=['recipe'],
        help='Command to execute'
    )
    parser.add_argument(
        'name',
        help='Exact name of the product or blueprint'
    )
    parser.add_argument(
        '--sde',
        required=True,
        help='Path to the SDE directory'
    )

    args = parser.parse_args()

    # Check SDE directory exists
    if not os.path.isdir(args.sde):
        print(f"Error: SDE directory '{args.sde}' does not exist", file=sys.stderr)
        sys.exit(1)

    # Initialize SDE
    sde = SDE(args.sde)

    # Find the type
    type_data = sde.get_type_by_name(args.name)

    if type_data is None:
        print(f"Error: Item '{args.name}' not found or not published", file=sys.stderr)
        sys.exit(1)

    # Determine if it's a blueprint or product
    # A blueprint has category 'Blueprint'
    group_id = type_data.get('groupID')
    group = sde.get_group(group_id) if group_id else None
    category_id = group.get('categoryID') if group else None
    category = sde.get_category(category_id) if category_id else None

    is_blueprint = category and category['categoryName'] == 'Blueprint'

    if is_blueprint:
        # Find the product this blueprint produces
        bp_id = type_data['typeID']
        result = None
        for act_id in [1, 11]:  # Manufacturing, Reactions
            recipe = sde.get_blueprint_recipe(bp_id, act_id)
            if recipe:
                result = (bp_id, act_id)
                break

        if result is None:
            print(f"Error: Blueprint '{args.name}' has no manufacturing or reactions recipe", file=sys.stderr)
            sys.exit(1)

        bp_id, activity_id = result
        recipe = sde.get_blueprint_recipe(bp_id, activity_id)
        product_type = sde.get_type(recipe['productTypeID'])

        if product_type is None:
            print(f"Error: Could not find product for blueprint '{args.name}'", file=sys.stderr)
            sys.exit(1)

        # Format recipe for the product
        output = format_recipe(sde, product_type)
    else:
        # It's a product
        output = format_recipe(sde, type_data)

    print(output)


if __name__ == '__main__':
    main()
