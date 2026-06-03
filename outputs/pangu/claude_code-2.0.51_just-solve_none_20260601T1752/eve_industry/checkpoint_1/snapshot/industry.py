#!/usr/bin/env python3
"""
EVE Online Industry Recipe Tool
Parses the SDE and emits a deterministic recipe report for a target product or blueprint.
"""

import argparse
import csv
import math
import os
import sys
import bz2
from io import TextIOWrapper
from collections import defaultdict


def read_bz2_csv(filepath):
    """Read a BZ2-compressed CSV file and return a list of dictionaries."""
    with bz2.open(filepath, 'rt', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        return list(reader)


def read_csv(filepath):
    """Read a plain CSV file and return a list of dictionaries."""
    with open(filepath, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        return list(reader)


def read_yaml(filepath):
    """Read a plain YAML file."""
    # Simple YAML parser for key-value pairs
    with open(filepath, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    result = {}
    for line in lines:
        line = line.strip()
        if ':' in line and not line.startswith('#'):
            key, value = line.split(':', 1)
            key = key.strip()
            value = value.strip()
            # Try to convert to float if possible
            try:
                result[key] = float(value)
            except ValueError:
                result[key] = value
    return result


class SDEParser:
    """Parses SDE data files and builds lookup structures."""

    def __init__(self, sde_dir):
        self.sde_dir = sde_dir

        # Data storage
        self.types = {}  # typeID -> {typeName, groupID, volume, published, ...}
        self.types_by_name = {}  # typeName -> typeID
        self.groups = {}  # groupID -> {groupName, categoryID, ...}
        self.categories = {}  # categoryID -> categoryName
        self.market_groups = {}  # marketGroupID -> {marketGroupName, parentGroupID, ...}
        self.meta_groups = {}  # metaGroupID -> metaGroupName
        self.meta_types = defaultdict(list)  # typeID -> [(parentTypeID, metaGroupID), ...]

        # Industry data
        self.activities = {}  # (typeID, activityID) -> {activityName, time, ...}
        self.products = defaultdict(list)  # (typeID, activityID) -> [{productTypeID, quantity}, ...]
        self.materials = defaultdict(list)  # (typeID, activityID) -> [{materialTypeID, quantity}, ...]
        self.activity_products_map = {}  # typeID -> activityID (manufacturing/reactions)

        # Ship volumes
        self.ship_volumes = {}

        self._parse_all()

    def _parse_all(self):
        """Parse all SDE files."""
        self._parse_inv_types()
        self._parse_inv_groups()
        self._parse_inv_categories()
        self._parse_inv_market_groups()
        self._parse_inv_meta_groups()
        self._parse_inv_meta_types()
        self._parse_industry_activity()
        self._parse_industry_activity_products()
        self._parse_industry_activity_materials()
        self._parse_ram_activities()
        self._parse_ship_volumes()
        self._build_activity_lookup()

    def _parse_inv_types(self):
        """Parse invTypes.csv.bz2"""
        data = read_bz2_csv(os.path.join(self.sde_dir, 'invTypes.csv.bz2'))
        for row in data:
            if int(row.get('published', 0)) != 1:
                continue
            type_id = int(row['typeID'])
            type_name = row['typeName']
            volume = row.get('volume', '0')
            group_id = int(row['groupID'])

            self.types[type_id] = {
                'typeID': type_id,
                'typeName': type_name,
                'groupID': group_id,
                'volume': volume,
                'marketGroupID': row.get('marketGroupID', ''),
            }
            self.types_by_name[type_name] = type_id

    def _parse_inv_groups(self):
        """Parse invGroups.csv.bz2"""
        data = read_bz2_csv(os.path.join(self.sde_dir, 'invGroups.csv.bz2'))
        for row in data:
            if int(row.get('published', 0)) != 1:
                continue
            group_id = int(row['groupID'])
            category_id = int(row['categoryID'])
            group_name = row['groupName']

            self.groups[group_id] = {
                'groupID': group_id,
                'groupName': group_name,
                'categoryID': category_id,
            }

    def _parse_inv_categories(self):
        """Parse invCategories.csv.bz2"""
        data = read_bz2_csv(os.path.join(self.sde_dir, 'invCategories.csv.bz2'))
        for row in data:
            if int(row.get('published', 0)) != 1:
                continue
            category_id = int(row['categoryID'])
            category_name = row['categoryName']
            self.categories[category_id] = category_name

    def _parse_inv_market_groups(self):
        """Parse invMarketGroups.csv.bz2"""
        data = read_bz2_csv(os.path.join(self.sde_dir, 'invMarketGroups.csv.bz2'))
        for row in data:
            market_group_id = row['marketGroupID']
            if not market_group_id:
                continue
            market_group_id = int(market_group_id)
            parent_id = row.get('parentGroupID', '')
            parent_id = int(parent_id) if parent_id and parent_id != 'None' else None
            market_group_name = row['marketGroupName']

            self.market_groups[market_group_id] = {
                'marketGroupID': market_group_id,
                'parentGroupID': parent_id,
                'marketGroupName': market_group_name,
            }

    def _parse_inv_meta_groups(self):
        """Parse invMetaGroups.csv.bz2"""
        data = read_bz2_csv(os.path.join(self.sde_dir, 'invMetaGroups.csv.bz2'))
        for row in data:
            if int(row.get('published', 0)) != 1:
                continue
            meta_group_id = int(row['metaGroupID'])
            meta_group_name = row['metaGroupName']
            self.meta_groups[meta_group_id] = meta_group_name

    def _parse_inv_meta_types(self):
        """Parse invMetaTypes.csv.bz2"""
        data = read_bz2_csv(os.path.join(self.sde_dir, 'invMetaTypes.csv.bz2'))
        for row in data:
            type_id = int(row['typeID'])
            parent_type_id = row.get('parentTypeID', '')
            parent_type_id = int(parent_type_id) if parent_type_id and parent_type_id != 'None' else None
            meta_group_id = row.get('metaGroupID', '')
            meta_group_id = int(meta_group_id) if meta_group_id and meta_group_id != 'None' else None

            if parent_type_id:
                self.meta_types[type_id].append({
                    'parentTypeID': parent_type_id,
                    'metaGroupID': meta_group_id
                })

    def _parse_industry_activity(self):
        """Parse industryActivity.csv.bz2"""
        data = read_bz2_csv(os.path.join(self.sde_dir, 'industryActivity.csv.bz2'))
        for row in data:
            type_id = int(row['typeID'])
            activity_id = int(row['activityID'])
            time_val = int(row['time'])

            self.activities[(type_id, activity_id)] = {
                'typeID': type_id,
                'activityID': activity_id,
                'time': time_val,
            }

    def _parse_industry_activity_products(self):
        """Parse industryActivityProducts.csv.bz2"""
        data = read_bz2_csv(os.path.join(self.sde_dir, 'industryActivityProducts.csv.bz2'))
        for row in data:
            type_id = int(row['typeID'])
            activity_id = int(row['activityID'])
            product_type_id = int(row['productTypeID'])
            quantity = int(row['quantity'])

            self.products[(type_id, activity_id)].append({
                'productTypeID': product_type_id,
                'quantity': quantity,
            })

    def _parse_industry_activity_materials(self):
        """Parse industryActivityMaterials.csv.bz2"""
        data = read_bz2_csv(os.path.join(self.sde_dir, 'industryActivityMaterials.csv.bz2'))
        for row in data:
            type_id = int(row['typeID'])
            activity_id = int(row['activityID'])
            material_type_id = int(row['materialTypeID'])
            quantity = int(row['quantity'])

            self.materials[(type_id, activity_id)].append({
                'materialTypeID': material_type_id,
                'quantity': quantity,
            })

    def _parse_ram_activities(self):
        """Parse ramActivities.csv.bz2 - maps activityID to activityName"""
        data = read_bz2_csv(os.path.join(self.sde_dir, 'ramActivities.csv.bz2'))
        self.activity_names = {}
        for row in data:
            activity_id = int(row['activityID'])
            activity_name = row['activityName']
            self.activity_names[activity_id] = activity_name

    def _parse_ship_volumes(self):
        """Parse ship_volumes.yaml"""
        filepath = os.path.join(self.sde_dir, 'ship_volumes.yaml')
        if os.path.exists(filepath):
            self.ship_volumes = read_yaml(filepath)

    def _build_activity_lookup(self):
        """Build lookup from typeID to manufacturing/reactions activity"""
        for (type_id, activity_id), activity_data in self.activities.items():
            activity_name = self.activity_names.get(activity_id, '')
            if activity_name in ('Manufacturing', 'Reactions'):
                self.activity_products_map[type_id] = activity_id

        # Also build reverse lookup: product_type_id -> activity_id
        # This lets us check if a material is buildable by seeing if it's a product
        self.product_to_activity = {}
        for (type_id, activity_id), products in self.products.items():
            activity_name = self.activity_names.get(activity_id, '')
            if activity_name in ('Manufacturing', 'Reactions'):
                for product in products:
                    self.product_to_activity[product['productTypeID']] = activity_id

    def get_type_by_name(self, name):
        """Get type info by exact name."""
        type_id = self.types_by_name.get(name)
        if type_id:
            return self.types[type_id]
        return None

    def get_blueprint_from_product(self, product_type_id):
        """Find the blueprint that produces the given product type."""
        for (bp_type_id, activity_id), products in self.products.items():
            for product in products:
                if product['productTypeID'] == product_type_id:
                    return bp_type_id
        return None

    def get_product_from_blueprint(self, bp_type_id):
        """Get the product info for a blueprint."""
        activity_id = self.activity_products_map.get(bp_type_id)
        if not activity_id:
            return None, None

        products = self.products.get((bp_type_id, activity_id))
        if not products:
            return None, None

        activity_info = self.activities.get((bp_type_id, activity_id), {})
        return products[0], activity_info

    def get_materials(self, type_id):
        """Get materials for manufacturing/reactions for a type."""
        activity_id = self.activity_products_map.get(type_id)
        if not activity_id:
            return []

        return self.materials.get((type_id, activity_id), [])

    def get_tech_level(self, type_id):
        """Determine tech level of an item based on meta types."""
        meta_groups = self.meta_types.get(type_id, [])
        if not meta_groups:
            return "Tech I"

        # Check the meta group
        for meta_info in meta_groups:
            meta_group_id = meta_info.get('metaGroupID')
            if meta_group_id:
                meta_group_name = self.meta_groups.get(meta_group_id, '')
                if meta_group_name == 'Technology II':
                    return "Tech II"
                elif meta_group_name == 'Technology III':
                    return "Tech III"

        return "Tech I"

    def get_group_hierarchy(self, group_id):
        """Get the full group hierarchy as a list from category to group."""
        hierarchy = []

        # Get category
        group = self.groups.get(group_id)
        if not group:
            return hierarchy

        category_id = group['categoryID']
        category_name = self.categories.get(category_id, '')
        if category_name:
            hierarchy.append(category_name)

        group_name = group['groupName']
        if group_name:
            hierarchy.append(group_name)

        return hierarchy

    def get_market_group_path(self, type_id):
        """Get the market group path for a type."""
        type_info = self.types.get(type_id)
        if not type_info:
            return None

        market_group_id = type_info.get('marketGroupID')
        if not market_group_id:
            return None
        # Convert to int if string
        if isinstance(market_group_id, str):
            try:
                market_group_id = int(market_group_id)
            except ValueError:
                return None

        # Build path from leaf to root
        path = []
        while market_group_id:
            market_group = self.market_groups.get(market_group_id)
            if not market_group:
                break
            path.insert(0, market_group['marketGroupName'])
            next_id = market_group.get('parentGroupID')
            if isinstance(next_id, str):
                try:
                    market_group_id = int(next_id)
                except ValueError:
                    break
            else:
                market_group_id = next_id

        return ' > '.join(path) if path else None

    def get_volume(self, type_id, group_id):
        """Get volume with ship adjustment."""
        type_info = self.types.get(type_id)
        if not type_info:
            return None

        group = self.groups.get(group_id, {})
        group_name = group.get('groupName', '')

        # Check if this is a ship type
        if group_name in self.ship_volumes:
            return self.ship_volumes[group_name]

        # Use regular volume
        volume_str = type_info.get('volume', '0')
        try:
            return float(volume_str)
        except ValueError:
            return 0.0

    def is_buildable(self, material_type_id):
        """Check if a material type can be produced via industry."""
        # A material is buildable if it can be produced as a product
        # (via Manufacturing or Reactions)
        activity_id = self.product_to_activity.get(material_type_id)
        return activity_id is not None


def resolve_material_quantities(sde, material_list, resolved=None):
    """
    Resolve materials for a product, handling quantities.
    Returns a dict of material_type_id -> total_quantity required.
    """
    if resolved is None:
        resolved = defaultdict(int)

    for material in material_list:
        material_type_id = material['materialTypeID']
        quantity = material['quantity']
        resolved[material_type_id] += quantity

        # If this material is also buildable, we don't need to include its sub-materials
        # as per the spec, we just check if it's buildable or not

    return resolved


def format_recipe(sde, target_name):
    """Format the full recipe output for a target product or blueprint."""

    # Find the target type
    type_info = sde.get_type_by_name(target_name)
    if not type_info:
        return f"Error: Item '{target_name}' not found or not published."

    type_id = type_info['typeID']
    type_name = type_info['typeName']
    group_id = type_info['groupID']

    # Check if this is a blueprint (ends with ' Blueprint')
    is_bp = type_name.endswith(' Blueprint')

    if is_bp:
        # It's a blueprint
        bp_type_id = type_id
        product_info, activity_info = sde.get_product_from_blueprint(bp_type_id)

        if not product_info:
            return f"Error: No product found for blueprint '{target_name}'."

        product_type_id = product_info['productTypeID']
        product_info_type = sde.types.get(product_type_id, {})
        product_name = product_info_type.get('typeName', 'Unknown')

        output_quantity = product_info['quantity']
        run_time = activity_info.get('time', 0)
        run_time = math.ceil(run_time / 60.0)  # Convert seconds to minutes, rounded up
        activity_name = sde.activity_names.get(activity_info.get('activityID', 0), 'Manufacturing')

        # Display the actual product name
        display_name = product_name
        display_id = product_type_id
    else:
        # It's a product, find its blueprint
        bp_type_id = sde.get_blueprint_from_product(type_id)

        if not bp_type_id:
            return f"Error: No blueprint found for product '{target_name}'."

        bp_info = sde.types.get(bp_type_id, {})
        bp_name = bp_info.get('typeName', 'Unknown')

        product_info, activity_info = sde.get_product_from_blueprint(bp_type_id)

        if not product_info:
            return f"Error: No product info found for blueprint '{bp_name}'."

        output_quantity = product_info['quantity']
        run_time = activity_info.get('time', 0)
        run_time = math.ceil(run_time / 60.0)  # Convert seconds to minutes, rounded up
        activity_name = sde.activity_names.get(activity_info.get('activityID', 0), 'Manufacturing')

        display_name = type_name
        display_id = type_id

    # Get materials
    materials = sde.get_materials(bp_type_id)

    # Build output lines
    lines = []

    # ITEM line
    lines.append(f"ITEM: {display_name} ({display_id})")

    # Group line
    group_hierarchy = sde.get_group_hierarchy(group_id)
    lines.append(f"Group: {' > '.join(group_hierarchy)}")

    # Market Group line
    market_path = sde.get_market_group_path(display_id)
    lines.append(f"Market Group: {market_path}")

    # Tech Level
    tech_level = sde.get_tech_level(display_id)
    lines.append(f"Tech Level: {tech_level}")

    # Volume
    volume = sde.get_volume(display_id, group_id)
    lines.append(f"Volume: {volume:.2f}")

    lines.append("")
    lines.append("Recipe:")
    lines.append(f"Activity: {activity_name}")
    lines.append(f"Output Quantity: {output_quantity}")
    lines.append(f"Run Time: {run_time}")

    # Materials table
    lines.append("| Item | Quantity | Buildable |")
    lines.append("|:-:|:---:|---:|")

    # Sort materials alphabetically by item name (case-insensitive)
    materials_with_names = []
    for m in materials:
        mat_type_id = m['materialTypeID']
        mat_type_info = sde.types.get(mat_type_id, {})
        mat_name = mat_type_info.get('typeName', 'Unknown')
        mat_quantity = m['quantity']
        mat_buildable = sde.is_buildable(mat_type_id)
        materials_with_names.append((mat_name.lower(), mat_name, mat_quantity, mat_buildable))

    materials_with_names.sort(key=lambda x: x[0])

    for _, name, quantity, buildable in materials_with_names:
        buildable_str = 'Yes' if buildable else 'No'
        lines.append(f"| {name} | {quantity} | {buildable_str} |")

    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(
        description='EVE Online Industry Recipe Tool',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  python industry.py recipe Naga --sde /path/to/sde
  python industry.py recipe "Barrage L Blueprint" --sde /path/to/sde
  python industry.py recipe Fernite Carbide --sde /path/to/sde
'''
    )

    subparsers = parser.add_subparsers(dest='command', help='Command to run')

    recipe_parser = subparsers.add_parser('recipe', help='Show recipe for a product or blueprint')
    recipe_parser.add_argument('name', help='Product or blueprint name (exact, case-sensitive)')
    recipe_parser.add_argument('--sde', required=True, help='Path to SDE directory')

    args = parser.parse_args()

    if args.command != 'recipe':
        parser.print_help()
        sys.exit(1)

    if not os.path.isdir(args.sde):
        print(f"Error: SDE directory '{args.sde}' does not exist.", file=sys.stderr)
        sys.exit(1)

    # Parse SDE
    sde = SDEParser(args.sde)

    # Format and output recipe
    result = format_recipe(sde, args.name)
    print(result)


if __name__ == '__main__':
    main()
