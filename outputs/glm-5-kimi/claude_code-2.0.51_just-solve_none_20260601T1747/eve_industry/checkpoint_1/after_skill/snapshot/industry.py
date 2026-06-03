#!/usr/bin/env python3
"""
EVE Online Industry Recipe Planner
Parses the Static Data Export (SDE) and emits a deterministic recipe report.
"""

import argparse
import bz2
import csv
import math
import os
import sys
from collections import defaultdict
from io import StringIO

import yaml


def load_bz2_csv(filepath):
    """Load a bz2-compressed CSV file and return list of dicts."""
    with bz2.open(filepath, 'rt', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        return list(reader)


def load_yaml(filepath):
    """Load a YAML file."""
    with open(filepath, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


class SDEDatabase:
    """Container for all SDE data."""

    def __init__(self, sde_dir):
        self.sde_dir = sde_dir
        self._load_all()

    def _load_all(self):
        # Load item/type data
        self.inv_types = self._load_csv('invTypes.csv.bz2')
        self.inv_groups = self._load_csv('invGroups.csv.bz2')
        self.inv_categories = self._load_csv('invCategories.csv.bz2')
        self.inv_market_groups = self._load_csv('invMarketGroups.csv.bz2')
        self.inv_meta_types = self._load_csv('invMetaTypes.csv.bz2')
        self.inv_meta_groups = self._load_csv('invMetaGroups.csv.bz2')

        # Load industry data
        self.industry_activity = self._load_csv('industryActivity.csv.bz2')
        self.industry_activity_products = self._load_csv('industryActivityProducts.csv.bz2')
        self.industry_activity_materials = self._load_csv('industryActivityMaterials.csv.bz2')
        self.industry_activity_skills = self._load_csv('industryActivitySkills.csv.bz2')
        self.industry_activity_probabilities = self._load_csv('industryActivityProbabilities.csv.bz2')
        self.ram_activities = self._load_csv('ramActivities.csv.bz2')

        # Load ship volumes
        self.ship_volumes = load_yaml(os.path.join(self.sde_dir, 'ship_volumes.yaml'))

        # Build indexes
        self._build_indexes()

    def _load_csv(self, filename):
        """Load a CSV file from the SDE directory."""
        filepath = os.path.join(self.sde_dir, filename)
        return load_bz2_csv(filepath)

    def _build_indexes(self):
        """Build various indexes for fast lookups."""
        # type_id -> type data (published only)
        self.types_by_id = {}
        self.types_by_name = {}
        for row in self.inv_types:
            if row.get('published') == '1':
                type_id = int(row['typeID'])
                self.types_by_id[type_id] = row
                type_name = row['typeName']
                self.types_by_name[type_name] = row

        # group_id -> group data
        self.groups_by_id = {}
        for row in self.inv_groups:
            group_id = int(row['groupID'])
            self.groups_by_id[group_id] = row

        # category_id -> category data
        self.categories_by_id = {}
        for row in self.inv_categories:
            category_id = int(row['categoryID'])
            self.categories_by_id[category_id] = row

        # market_group_id -> market group data
        self.market_groups_by_id = {}
        for row in self.inv_market_groups:
            market_group_id = int(row['marketGroupID'])
            self.market_groups_by_id[market_group_id] = row

        # meta_group_id -> meta group data
        self.meta_groups_by_id = {}
        for row in self.inv_meta_groups:
            meta_group_id = int(row['metaGroupID'])
            self.meta_groups_by_id[meta_group_id] = row

        # type_id -> meta_group_id (from invMetaTypes)
        self.type_meta_group = {}
        for row in self.inv_meta_types:
            type_id = int(row['typeID'])
            meta_group_id = int(row['metaGroupID'])
            self.type_meta_group[type_id] = meta_group_id

        # activity_id -> activity name
        self.activities_by_id = {}
        for row in self.ram_activities:
            activity_id = int(row['activityID'])
            self.activities_by_id[activity_id] = row['activityName']

        # Build blueprint product lookup: product_type_id -> blueprint_type_id
        self.product_to_blueprint = {}
        self.blueprint_to_products = defaultdict(list)
        for row in self.industry_activity_products:
            bp_type_id = int(row['typeID'])
            activity_id = int(row['activityID'])
            product_type_id = int(row['productTypeID'])
            quantity = int(row['quantity'])
            self.product_to_blueprint[product_type_id] = (bp_type_id, activity_id)
            self.blueprint_to_products[bp_type_id].append({
                'activity_id': activity_id,
                'product_type_id': product_type_id,
                'quantity': quantity
            })

        # Build materials lookup: (blueprint_type_id, activity_id) -> list of materials
        self.blueprint_materials = defaultdict(list)
        for row in self.industry_activity_materials:
            bp_type_id = int(row['typeID'])
            activity_id = int(row['activityID'])
            material_type_id = int(row['materialTypeID'])
            quantity = int(row['quantity'])
            self.blueprint_materials[(bp_type_id, activity_id)].append({
                'material_type_id': material_type_id,
                'quantity': quantity
            })

        # Build time lookup: (blueprint_type_id, activity_id) -> time
        self.blueprint_time = {}
        for row in self.industry_activity:
            bp_type_id = int(row['typeID'])
            activity_id = int(row['activityID'])
            time = int(row['time'])
            self.blueprint_time[(bp_type_id, activity_id)] = time

    def get_type_by_name(self, name):
        """Get type data by exact name match."""
        return self.types_by_name.get(name)

    def get_type_by_id(self, type_id):
        """Get type data by type ID."""
        return self.types_by_id.get(type_id)

    def get_group_and_category(self, type_id):
        """Get group and category names for a type."""
        type_data = self.types_by_id.get(type_id)
        if not type_data:
            return None, None

        group_id = int(type_data['groupID'])
        group_data = self.groups_by_id.get(group_id)
        if not group_data:
            return None, None

        group_name = group_data['groupName']

        category_id = int(group_data['categoryID'])
        category_data = self.categories_by_id.get(category_id)
        category_name = category_data['categoryName'] if category_data else None

        return category_name, group_name

    def get_market_group_path(self, type_id):
        """Get the full market group path for a type."""
        type_data = self.types_by_id.get(type_id)
        if not type_data or not type_data.get('marketGroupID'):
            return None

        market_group_id = int(type_data['marketGroupID'])
        path = []

        while market_group_id:
            mg_data = self.market_groups_by_id.get(market_group_id)
            if not mg_data:
                break
            path.append(mg_data['marketGroupName'])
            parent_id = mg_data.get('parentGroupID')
            market_group_id = int(parent_id) if parent_id and parent_id != 'None' else None

        return ' > '.join(reversed(path)) if path else None

    def get_tech_level(self, type_id):
        """Get the tech level for a type."""
        meta_group_id = self.type_meta_group.get(type_id)
        if meta_group_id:
            mg_data = self.meta_groups_by_id.get(meta_group_id)
            if mg_data:
                name = mg_data['metaGroupName']
                if name == 'Tech I':
                    return 'Tech I'
                elif name == 'Tech II':
                    return 'Tech II'
                elif name == 'Tech III':
                    return 'Tech III'
        return 'Tech I'

    def get_volume(self, type_id):
        """Get the packaged volume for a type."""
        type_data = self.types_by_id.get(type_id)
        if not type_data:
            return 0.0

        # Check if this is a ship (look up by group name)
        group_id = int(type_data['groupID'])
        group_data = self.groups_by_id.get(group_id)
        if group_data:
            group_name = group_data['groupName']
            # Check if this group has a packaged volume
            if self.ship_volumes and group_name in self.ship_volumes:
                return float(self.ship_volumes[group_name])

        # Use the volume from invTypes
        volume = type_data.get('volume', '0')
        return float(volume) if volume else 0.0

    def is_ship(self, type_id):
        """Check if a type is a ship (has packaged volume)."""
        type_data = self.types_by_id.get(type_id)
        if not type_data:
            return False

        group_id = int(type_data['groupID'])
        group_data = self.groups_by_id.get(group_id)
        if group_data:
            group_name = group_data['groupName']
            return group_name in self.ship_volumes if self.ship_volumes else False
        return False

    def get_blueprint_for_product(self, product_type_id):
        """Get the blueprint type ID that produces this product."""
        return self.product_to_blueprint.get(product_type_id)

    def get_recipe_for_blueprint(self, blueprint_type_id, activity_id):
        """Get recipe details for a blueprint and activity."""
        materials = self.blueprint_materials.get((blueprint_type_id, activity_id), [])
        time = self.blueprint_time.get((blueprint_type_id, activity_id), 0)

        # Get output quantity
        products = self.blueprint_to_products.get(blueprint_type_id, [])
        output_quantity = 1
        for p in products:
            if p['activity_id'] == activity_id:
                output_quantity = p['quantity']
                break

        return {
            'materials': materials,
            'time': time,
            'output_quantity': output_quantity
        }

    def is_buildable(self, type_id):
        """Check if a type can be produced via industry (has a blueprint)."""
        return type_id in self.product_to_blueprint

    def get_activity_name(self, activity_id):
        """Get the activity name for an activity ID."""
        return self.activities_by_id.get(activity_id, 'Unknown')


def format_recipe_report(db, target_name):
    """Generate the formatted recipe report."""
    # First, check if the target is a product or blueprint name
    type_data = db.get_type_by_name(target_name)

    if not type_data:
        return f"Error: Item '{target_name}' not found in SDE."

    type_id = int(type_data['typeID'])

    # Determine if this is a blueprint or a product
    is_blueprint = 'Blueprint' in type_data['typeName'] and type_data['typeName'].endswith('Blueprint')

    if is_blueprint:
        # This is a blueprint - find its product
        products = db.blueprint_to_products.get(type_id, [])
        if not products:
            return f"Error: Blueprint '{target_name}' has no products."

        # For manufacturing/reactions, we look for activity 1 or 11
        # Activity 1 = Manufacturing, Activity 11 = Reactions
        activity_id = None
        for p in products:
            if p['activity_id'] in (1, 11):
                activity_id = p['activity_id']
                break

        if activity_id is None:
            activity_id = products[0]['activity_id']

        blueprint_type_id = type_id
        product_info = None
        for p in products:
            if p['activity_id'] == activity_id:
                product_info = p
                break

        if not product_info:
            product_info = products[0]

        product_type_id = product_info['product_type_id']
        product_type_data = db.get_type_by_id(product_type_id)
        if not product_type_data:
            return f"Error: Product type {product_type_id} not found."

        actual_product_name = product_type_data['typeName']
        actual_product_id = product_type_id
    else:
        # This is a product - find its blueprint
        bp_info = db.get_blueprint_for_product(type_id)
        if not bp_info:
            return f"Error: No blueprint found for product '{target_name}'."

        blueprint_type_id, activity_id = bp_info
        actual_product_name = target_name
        actual_product_id = type_id

    # Get product details
    product_type_data = db.get_type_by_id(actual_product_id)

    # Get recipe data
    recipe = db.get_recipe_for_blueprint(blueprint_type_id, activity_id)

    # Get category and group
    category_name, group_name = db.get_group_and_category(actual_product_id)

    # Get market group path
    market_group_path = db.get_market_group_path(actual_product_id)

    # Get tech level
    tech_level = db.get_tech_level(actual_product_id)

    # Get volume
    volume = db.get_volume(actual_product_id)

    # Get activity name
    activity_name = db.get_activity_name(activity_id)

    # Format volume: show up to 3 decimal places, strip trailing zeros but keep at least 2
    volume_str = f"{volume:.3f}".rstrip('0')
    if volume_str.endswith('.'):
        volume_str += '00'
    elif len(volume_str.split('.')[1]) < 2:
        volume_str += '0'

    # Format the output
    lines = []
    lines.append(f"ITEM: {actual_product_name} ({actual_product_id})")
    lines.append(f"Group: {category_name} > {group_name}")
    lines.append(f"Market Group: {market_group_path if market_group_path else 'None'}")
    lines.append(f"Tech Level: {tech_level}")
    lines.append(f"Volume: {volume_str}")
    lines.append("")
    lines.append("Recipe:")
    lines.append(f"Activity: {activity_name}")
    lines.append(f"Output Quantity: {recipe['output_quantity']}")
    lines.append(f"Run Time: {math.ceil(recipe['time'] / 60)}")

    # Materials table
    lines.append("| Item | Quantity | Buildable |")
    lines.append("|:-:|:---:|---:|")

    # Sort materials alphabetically by name (case-insensitive)
    materials = []
    for mat in recipe['materials']:
        mat_type_id = mat['material_type_id']
        mat_type_data = db.get_type_by_id(mat_type_id)
        if mat_type_data:
            mat_name = mat_type_data['typeName']
            quantity = mat['quantity']
            buildable = 'Yes' if db.is_buildable(mat_type_id) else 'No'
            materials.append((mat_name, quantity, buildable))

    # Sort case-insensitive
    materials.sort(key=lambda x: x[0].lower())

    for mat_name, quantity, buildable in materials:
        lines.append(f"| {mat_name} | {quantity} | {buildable} |")

    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description='EVE Online Industry Recipe Planner')
    subparsers = parser.add_subparsers(dest='command', help='Available commands')

    recipe_parser = subparsers.add_parser('recipe', help='Generate recipe report for a product or blueprint')
    recipe_parser.add_argument('name', help='Exact product or blueprint name')
    recipe_parser.add_argument('--sde', required=True, help='Path to SDE directory')

    args = parser.parse_args()

    if args.command == 'recipe':
        if not os.path.isdir(args.sde):
            print(f"Error: SDE directory '{args.sde}' not found.", file=sys.stderr)
            sys.exit(1)

        try:
            db = SDEDatabase(args.sde)
            report = format_recipe_report(db, args.name)
            print(report)
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
