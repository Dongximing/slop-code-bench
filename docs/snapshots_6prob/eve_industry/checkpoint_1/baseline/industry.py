#!/usr/bin/env python3
"""
EVE Online Industry Recipe Planner
Parses SDE data to generate deterministic recipe reports for products or blueprints.
"""

import argparse
import bz2
import csv
import math
import sys
import yaml
from collections import defaultdict
from pathlib import Path


class SDEParser:
    """Parser for EVE Online Static Data Export files."""

    def __init__(self, sde_dir: str):
        self.sde_dir = Path(sde_dir)
        self.item_by_id = {}
        self.item_by_name = {}
        self.group_by_id = {}
        self.category_by_id = {}
        self.market_group_by_id = {}
        self.market_group_parent = {}
        self.ship_volumes = {}

    def load_all(self):
        """Load all SDE data files."""
        self._load_types()
        self._load_groups()
        self._load_categories()
        self._load_market_groups()
        self._load_ship_volumes()

    def _load_csv(self, filename: str, fieldnames: list):
        """Load a CSV file with BZ2 compression."""
        filepath = self.sde_dir / filename
        records = []
        with bz2.open(filepath, 'rt', encoding='utf-8') as f:
            reader = csv.DictReader(f, fieldnames=fieldnames, delimiter=',')
            for row in reader:
                records.append(row)
        return records

    def _load_types(self):
        """Load invTypes.csv."""
        filepath = self.sde_dir / 'invTypes.csv.bz2'
        with bz2.open(filepath, 'rt', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row['published'] == '1':
                    type_id = int(row['typeID'])
                    self.item_by_id[type_id] = {
                        'typeID': type_id,
                        'groupID': int(row['groupID']),
                        'typeName': row['typeName'],
                        'volume': float(row['volume']) if row['volume'] and row['volume'] != '0E-10' else 0.0,
                        'marketGroupID': int(row['marketGroupID']) if row['marketGroupID'] and row['marketGroupID'] != 'None' else None
                    }
                    self.item_by_name[row['typeName']] = self.item_by_id[type_id]

    def _load_groups(self):
        """Load invGroups.csv."""
        filepath = self.sde_dir / 'invGroups.csv.bz2'
        with bz2.open(filepath, 'rt', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row['published'] == '1':
                    group_id = int(row['groupID'])
                    self.group_by_id[group_id] = {
                        'groupID': group_id,
                        'categoryID': int(row['categoryID']),
                        'groupName': row['groupName']
                    }

    def _load_categories(self):
        """Load invCategories.csv."""
        filepath = self.sde_dir / 'invCategories.csv.bz2'
        with bz2.open(filepath, 'rt', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row['published'] == '1':
                    category_id = int(row['categoryID'])
                    self.category_by_id[category_id] = {
                        'categoryID': category_id,
                        'categoryName': row['categoryName']
                    }

    def _load_market_groups(self):
        """Load invMarketGroups.csv."""
        filepath = self.sde_dir / 'invMarketGroups.csv.bz2'
        with bz2.open(filepath, 'rt', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                market_group_id = int(row['marketGroupID'])
                parent_id = row['parentGroupID']
                self.market_group_by_id[market_group_id] = {
                    'marketGroupID': market_group_id,
                    'marketGroupName': row['marketGroupName'],
                    'parentMarketGroupID': int(parent_id) if parent_id and parent_id != 'None' else None
                }

    def _load_ship_volumes(self):
        """Load ship_volumes.yaml."""
        filepath = self.sde_dir / 'ship_volumes.yaml'
        with open(filepath, 'r', encoding='utf-8') as f:
            self.ship_volumes = yaml.safe_load(f)


class IndustryPlanner:
    """Main industry planning logic."""

    ACTIVITY_MANUFACTURING = 1
    ACTIVITY_REACTIONS = 11

    def __init__(self, sde_parser: SDEParser):
        self.parser = sde_parser
        self.industry_activities = self._load_industry_activities()
        self.industry_products = self._load_industry_products()
        self.industry_materials = self._load_industry_materials()
        self.industry_probabilities = self._load_industry_probabilities()

    def _load_industry_activities(self):
        """Load industryActivity.csv."""
        filepath = self.parser.sde_dir / 'industryActivity.csv.bz2'
        activities = {}
        with bz2.open(filepath, 'rt', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                key = (int(row['typeID']), int(row['activityID']))
                activities[key] = {
                    'time': int(row['time'])
                }
        return activities

    def _load_industry_products(self):
        """Load industryActivityProducts.csv."""
        filepath = self.parser.sde_dir / 'industryActivityProducts.csv.bz2'
        products = defaultdict(list)
        with bz2.open(filepath, 'rt', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                key = (int(row['typeID']), int(row['activityID']))
                products[key].append({
                    'productTypeID': int(row['productTypeID']),
                    'quantity': int(row['quantity'])
                })
        return products

    def _load_industry_materials(self):
        """Load industryActivityMaterials.csv."""
        filepath = self.parser.sde_dir / 'industryActivityMaterials.csv.bz2'
        materials = defaultdict(list)
        with bz2.open(filepath, 'rt', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                key = (int(row['typeID']), int(row['activityID']))
                materials[key].append({
                    'materialTypeID': int(row['materialTypeID']),
                    'quantity': int(row['quantity'])
                })
        return materials

    def _load_industry_probabilities(self):
        """Load industryActivityProbabilities.csv."""
        filepath = self.parser.sde_dir / 'industryActivityProbabilities.csv.bz2'
        probabilities = defaultdict(list)
        with bz2.open(filepath, 'rt', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                key = (int(row['typeID']), int(row['activityID']))
                probabilities[key].append({
                    'productTypeID': int(row['productTypeID']),
                    'probability': float(row['probability'])
                })
        return probabilities

    def find_product(self, name: str) -> dict:
        """Find a product by name. Returns item dict or None."""
        if name in self.parser.item_by_name:
            return self.parser.item_by_name[name]
        return None

    def find_blueprint(self, name: str) -> dict:
        """Find a blueprint by name. Returns item dict or None."""
        # Look for "X Blueprint" pattern
        if name.endswith(' Blueprint'):
            product_name = name[:-11]
            # Find the blueprint for this product
            for type_name, item in self.parser.item_by_name.items():
                if type_name == name:
                    return item
        return None

    def get_item_name(self, type_id: int) -> str:
        """Get item name by type ID."""
        if type_id in self.parser.item_by_id:
            return self.parser.item_by_id[type_id]['typeName']
        return None

    def get_group_hierarchy(self, group_id: int) -> list:
        """Get the group hierarchy from category to group."""
        if group_id not in self.parser.group_by_id:
            return []
        group = self.parser.group_by_id[group_id]
        category_id = group['categoryID']
        category_name = self.parser.category_by_id.get(category_id, {}).get('categoryName', 'Unknown')
        return [category_name, group['groupName']]

    def get_market_group_path(self, market_group_id: int) -> list:
        """Get the market group hierarchy path."""
        path = []
        current_id = market_group_id
        while current_id and current_id in self.parser.market_group_by_id:
            mg = self.parser.market_group_by_id[current_id]
            path.insert(0, mg['marketGroupName'])
            current_id = mg['parentMarketGroupID']
        return path

    def get_packaged_volume(self, item: dict) -> float:
        """Get packaged volume. For ships, use ship_volumes.yaml."""
        type_id = item['typeID']
        group_id = item['groupID']

        if group_id in self.parser.group_by_id:
            group = self.parser.group_by_id[group_id]
            group_name = group['groupName']
            category_id = group['categoryID']
            category = self.parser.category_by_id.get(category_id, {})

            # Check if it's a ship
            if category.get('categoryName') == 'Ship':
                if group_name in self.parser.ship_volumes:
                    return self.parser.ship_volumes[group_name]

        return item['volume']

    def get_tech_level(self, item: dict) -> str:
        """Determine tech level of an item."""
        type_id = item['typeID']
        group_id = item['groupID']

        # Check meta types for tech levels
        filepath = self.parser.sde_dir / 'invMetaTypes.csv.bz2'
        with bz2.open(filepath, 'rt', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if int(row['typeID']) == type_id:
                    meta_group_id = int(row['metaGroupID']) if row['metaGroupID'] else 0
                    # Meta groups: 1 = Tech I, 2 = Tech II, 3 = Tech III
                    if meta_group_id == 2:
                        return 'Tech II'
                    elif meta_group_id == 3:
                        return 'Tech III'

        # Check for blueprint pattern
        name = item['typeName']
        if 'Blueprint' in name:
            # Blueprint of certain types indicate tech level
            pass

        # Default to Tech I
        return 'Tech I'

    def is_buildable(self, material_type_id: int) -> bool:
        """Check if a material can be produced via industry."""
        # Check if the material has any industry activity (manufacturing or reactions)
        for (type_id, activity_id), materials in self.industry_materials.items():
            if type_id == material_type_id and activity_id in (self.ACTIVITY_MANUFACTURING, self.ACTIVITY_REACTIONS):
                return True
            if type_id == material_type_id and activity_id in (3, 4):  # Research activities
                return True

        # Also check if it has product entries (is a product of something)
        for (type_id, activity_id), products in self.industry_products.items():
            for p in products:
                if p['productTypeID'] == material_type_id:
                    return True

        return False

    def get_recipe_for_product(self, product_name: str) -> dict:
        """Get recipe for a product."""
        # First, try to find the product directly
        product = self.find_product(product_name)
        if not product:
            # Try to find blueprint instead
            blueprint = self.find_blueprint(product_name)
            if blueprint:
                product = blueprint

        if not product:
            return None

        type_id = product['typeID']
        name = product['typeName']

        # Check if this is a blueprint
        is_blueprint = 'Blueprint' in name

        # Determine the actual product ID we're producing
        if is_blueprint:
            # For blueprints, we need to find what they produce
            product_type_id = None
            for (bp_id, activity_id), products in self.industry_products.items():
                if bp_id == type_id and activity_id == self.ACTIVITY_MANUFACTURING:
                    for p in products:
                        product_type_id = p['productTypeID']
                        break
                if product_type_id:
                    break

            if not product_type_id:
                # This blueprint might be for reactions or something else
                # Try to find any product
                for (bp_id, activity_id), products in self.industry_products.items():
                    if bp_id == type_id:
                        product_type_id = products[0]['productTypeID']
                        break

            if not product_type_id:
                return None

            blueprint_type_id = type_id
            activity_type_id = self.ACTIVITY_MANUFACTURING
            activity_key = (type_id, activity_type_id)

        else:
            # This is a product, find its blueprint
            product_type_id = type_id
            blueprint_type_id = None
            activity_type_id = None

            # Find the blueprint that produces this
            for (bp_id, activity_id), products in self.industry_products.items():
                for p in products:
                    if p['productTypeID'] == type_id:
                        blueprint_type_id = bp_id
                        activity_type_id = activity_id
                        product_quantity = p['quantity']
                        break
                if blueprint_type_id:
                    break

            if not blueprint_type_id:
                # Maybe this is a reaction product? Check if it's produced via reactions
                # Check industryActivity for reactions
                for (mat_type_id, activity_id) in self.industry_activities:
                    if activity_id == self.ACTIVITY_REACTIONS:
                        # Check if this material is produced
                        mat_key = (mat_type_id, activity_id)
                        if mat_key in self.industry_products:
                            for p in self.industry_products[mat_key]:
                                if p['productTypeID'] == type_id:
                                    blueprint_type_id = mat_type_id
                                    activity_type_id = activity_id
                                    product_quantity = p['quantity']
                                    break
                        if blueprint_type_id:
                            break

            if not blueprint_type_id:
                return None

            activity_key = (blueprint_type_id, activity_type_id)

        # Get activity time
        activity_data = self.industry_activities.get(activity_key, {})
        run_time_seconds = activity_data.get('time', 0)
        run_time_minutes = math.ceil(run_time_seconds / 60)

        # Get materials
        materials_list = self.industry_materials.get(activity_key, [])

        # Get output quantity
        output_quantity = 1
        if activity_key in self.industry_products:
            products = self.industry_products[activity_key]
            for p in products:
                if is_blueprint or p['productTypeID'] == product_type_id:
                    output_quantity = p['quantity']
                    break

        # Get activity name
        activity_name = 'Manufacturing' if activity_type_id == self.ACTIVITY_MANUFACTURING else 'Reactions'

        return {
            'item': product,
            'item_type_id': product_type_id if product_type_id else blueprint_type_id,
            'blueprint_type_id': blueprint_type_id,
            'activity': activity_name,
            'activity_id': activity_type_id,
            'output_quantity': output_quantity,
            'run_time': run_time_minutes,
            'materials': materials_list
        }

    def format_recipe(self, recipe: dict) -> str:
        """Format the canonical recipe output."""
        item = recipe['item']
        type_id = item['typeID']
        name = item['typeName']

        # Get group hierarchy
        group_hierarchy = self.get_group_hierarchy(item['groupID'])
        group_str = ' > '.join(group_hierarchy) if group_hierarchy else 'Unknown'

        # Get market group path
        market_group_path = []
        if item['marketGroupID']:
            market_group_path = self.get_market_group_path(item['marketGroupID'])
        market_group_str = ' > '.join(market_group_path) if market_group_path else 'None'

        # Get tech level
        tech_level = self.get_tech_level(item)

        # Get volume
        volume = self.get_packaged_volume(item)

        # Build material rows
        material_rows = []
        for mat in recipe['materials']:
            mat_type_id = mat['materialTypeID']
            mat_name = self.get_item_name(mat_type_id) or f'Unknown ({mat_type_id})'
            mat_quantity = mat['quantity']
            mat_buildable = 'Yes' if self.is_buildable(mat_type_id) else 'No'
            material_rows.append((mat_name, mat_quantity, mat_buildable))

        # Sort alphabetically by item name (case-insensitive)
        material_rows.sort(key=lambda x: x[0].lower())

        # Build output
        lines = [
            f'ITEM: {name} ({type_id})',
            f'Group: {group_str}',
            f'Market Group: {market_group_str}',
            f'Tech Level: {tech_level}',
            f'Volume: {volume:.2f}',
            '',
            'Recipe:',
            f'Activity: {recipe["activity"]}',
            f'Output Quantity: {recipe["output_quantity"]}',
            f'Run Time: {recipe["run_time"]}',
            '| Item | Quantity | Buildable |',
            '|:-:|:---:|---:|'
        ]

        for mat_name, mat_quantity, mat_buildable in material_rows:
            lines.append(f'| {mat_name} | {mat_quantity} | {mat_buildable} |')

        return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(
        description='EVE Online Industry Recipe Planner',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''Examples:
  python industry.py recipe Naga --sde ./sde
  python industry.py recipe "Barrage L Blueprint" --sde ./sde
  python industry.py recipe Fernite Carbide --sde ./sde'''
    )

    subparsers = parser.add_subparsers(dest='command', required=True)

    recipe_parser = subparsers.add_parser('recipe', help='Generate recipe for a product or blueprint')
    recipe_parser.add_argument('name', help='Product or Blueprint name (exact, case-sensitive)')
    recipe_parser.add_argument('--sde', required=True, help='Path to SDE directory')

    args = parser.parse_args()

    # Initialize SDE parser
    sde = SDEParser(args.sde)
    sde.load_all()

    # Initialize planner
    planner = IndustryPlanner(sde)

    if args.command == 'recipe':
        recipe = planner.get_recipe_for_product(args.name)

        if not recipe:
            print(f'Error: Could not find product or blueprint "{args.name}"', file=sys.stderr)
            sys.exit(1)

        output = planner.format_recipe(recipe)
        print(output)


if __name__ == '__main__':
    main()
