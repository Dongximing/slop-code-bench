#!/usr/bin/env python3
"""
EVE Online Industry Recipe Planner

Parses the EVE Online Static Data Export (SDE) and outputs a deterministic
recipe report for a target product or blueprint.
"""

import argparse
import bz2
import csv
import math
import os
import sys
from pathlib import Path

import yaml


def load_bz2_csv(filepath):
    """Load a bz2-compressed CSV file and return list of dicts."""
    with bz2.open(filepath, 'rt', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        return list(reader)


def load_ship_volumes(filepath):
    """Load ship_volumes.yaml and return dict of group_name -> volume."""
    with open(filepath, 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f)
    return {k: v for k, v in data.items() if v is not None}


def load_sde(sde_dir):
    """Load all SDE data files into memory."""
    sde_path = Path(sde_dir)

    # Load item/type data
    inv_types = load_bz2_csv(sde_path / 'invTypes.csv.bz2')
    inv_groups = load_bz2_csv(sde_path / 'invGroups.csv.bz2')
    inv_categories = load_bz2_csv(sde_path / 'invCategories.csv.bz2')
    inv_market_groups = load_bz2_csv(sde_path / 'invMarketGroups.csv.bz2')
    inv_meta_types = load_bz2_csv(sde_path / 'invMetaTypes.csv.bz2')
    inv_meta_groups = load_bz2_csv(sde_path / 'invMetaGroups.csv.bz2')

    # Load industry data
    industry_activity = load_bz2_csv(sde_path / 'industryActivity.csv.bz2')
    industry_activity_products = load_bz2_csv(sde_path / 'industryActivityProducts.csv.bz2')
    industry_activity_materials = load_bz2_csv(sde_path / 'industryActivityMaterials.csv.bz2')
    ram_activities = load_bz2_csv(sde_path / 'ramActivities.csv.bz2')

    # Load ship volumes
    ship_volumes = load_ship_volumes(sde_path / 'ship_volumes.yaml')

    return {
        'inv_types': inv_types,
        'inv_groups': inv_groups,
        'inv_categories': inv_categories,
        'inv_market_groups': inv_market_groups,
        'inv_meta_types': inv_meta_types,
        'inv_meta_groups': inv_meta_groups,
        'industry_activity': industry_activity,
        'industry_activity_products': industry_activity_products,
        'industry_activity_materials': industry_activity_materials,
        'ram_activities': ram_activities,
        'ship_volumes': ship_volumes,
    }


def build_indexes(sde):
    """Build lookup indexes from SDE data."""
    # typeID -> type info
    types_by_id = {}
    types_by_name = {}
    for t in sde['inv_types']:
        type_id = t['typeID']
        types_by_id[type_id] = t
        types_by_name[t['typeName']] = t

    # groupID -> group info
    groups_by_id = {}
    for g in sde['inv_groups']:
        groups_by_id[g['groupID']] = g

    # categoryID -> category info
    categories_by_id = {}
    for c in sde['inv_categories']:
        categories_by_id[c['categoryID']] = c

    # marketGroupID -> market group info
    market_groups_by_id = {}
    for mg in sde['inv_market_groups']:
        market_groups_by_id[mg['marketGroupID']] = mg

    # typeID -> meta type info
    meta_types_by_id = {}
    for mt in sde['inv_meta_types']:
        meta_types_by_id[mt['typeID']] = mt

    # metaGroupID -> meta group info
    meta_groups_by_id = {}
    for mg in sde['inv_meta_groups']:
        meta_groups_by_id[mg['metaGroupID']] = mg

    # (blueprint_typeID, activityID) -> list of products
    products_by_blueprint = {}
    for p in sde['industry_activity_products']:
        key = (p['typeID'], p['activityID'])
        if key not in products_by_blueprint:
            products_by_blueprint[key] = []
        products_by_blueprint[key].append(p)

    # (blueprint_typeID, activityID) -> list of materials
    materials_by_blueprint = {}
    for m in sde['industry_activity_materials']:
        key = (m['typeID'], m['activityID'])
        if key not in materials_by_blueprint:
            materials_by_blueprint[key] = []
        materials_by_blueprint[key].append(m)

    # (blueprint_typeID, activityID) -> time
    time_by_blueprint = {}
    for a in sde['industry_activity']:
        key = (a['typeID'], a['activityID'])
        time_by_blueprint[key] = int(a['time'])

    # activityID -> activity name
    activity_names = {}
    for a in sde['ram_activities']:
        activity_names[a['activityID']] = a['activityName']

    # Build set of buildable type IDs (items that have a blueprint producing them)
    buildable_types = set()
    for products in products_by_blueprint.values():
        for p in products:
            buildable_types.add(p['productTypeID'])

    return {
        'types_by_id': types_by_id,
        'types_by_name': types_by_name,
        'groups_by_id': groups_by_id,
        'categories_by_id': categories_by_id,
        'market_groups_by_id': market_groups_by_id,
        'meta_types_by_id': meta_types_by_id,
        'meta_groups_by_id': meta_groups_by_id,
        'products_by_blueprint': products_by_blueprint,
        'materials_by_blueprint': materials_by_blueprint,
        'time_by_blueprint': time_by_blueprint,
        'activity_names': activity_names,
        'buildable_types': buildable_types,
        'ship_volumes': sde['ship_volumes'],
    }


def get_market_group_path(market_group_id, market_groups_by_id):
    """Build the full market group path from root to leaf."""
    if not market_group_id or market_group_id not in market_groups_by_id:
        return None

    path = []
    current_id = market_group_id

    while current_id and current_id in market_groups_by_id:
        mg = market_groups_by_id[current_id]
        path.append(mg['marketGroupName'])
        parent_id = mg['parentGroupID']
        if parent_id and parent_id != 'None':
            current_id = parent_id
        else:
            break

    return ' > '.join(reversed(path))


def get_tech_level(type_id, meta_types_by_id, meta_groups_by_id):
    """Determine the tech level of an item."""
    if type_id not in meta_types_by_id:
        return 'Tech I'

    meta_type = meta_types_by_id[type_id]
    meta_group_id = meta_type['metaGroupID']

    if meta_group_id not in meta_groups_by_id:
        return 'Tech I'

    meta_group_name = meta_groups_by_id[meta_group_id]['metaGroupName']

    if meta_group_name == 'Tech I':
        return 'Tech I'
    elif meta_group_name == 'Tech II':
        return 'Tech II'
    elif meta_group_name == 'Tech III':
        return 'Tech III'
    else:
        return 'Tech I'


def get_volume(type_info, group_info, category_info, ship_volumes):
    """Get the volume for an item, using packaged volume for ships."""
    # Check if this is a ship (categoryID 6)
    if category_info and category_info['categoryID'] == '6':
        # Use packaged volume for ships
        group_name = group_info['groupName']
        if group_name in ship_volumes:
            return ship_volumes[group_name]

    # Use volume from invTypes for non-ships
    volume_str = type_info.get('volume', '0')
    if volume_str and volume_str != 'None':
        try:
            return float(volume_str)
        except ValueError:
            return 0.0
    return 0.0


def find_blueprint_for_product(product_type_id, indexes):
    """Find the blueprint that produces a given product."""
    for key, products in indexes['products_by_blueprint'].items():
        for p in products:
            if p['productTypeID'] == product_type_id:
                return key[0], key[1]  # blueprint_type_id, activity_id
    return None, None


def is_blueprint(type_info, indexes):
    """Check if a type is a blueprint (categoryID 9)."""
    group_id = type_info['groupID']
    group_info = indexes['groups_by_id'].get(group_id)
    if group_info:
        category_id = group_info['categoryID']
        return category_id == '9'
    return False


def get_recipe(target_name, indexes):
    """Get recipe information for a target product or blueprint name."""
    # Look up the target type
    type_info = indexes['types_by_name'].get(target_name)
    if not type_info:
        return None, f"Error: '{target_name}' not found in SDE"

    # Check if published
    if type_info['published'] != '1':
        return None, f"Error: '{target_name}' is not published"

    type_id = type_info['typeID']

    # Determine if this is a blueprint or a product
    if is_blueprint(type_info, indexes):
        # It's a blueprint - find its product
        blueprint_type_id = type_id

        # Check for manufacturing (activity 1) first
        products = indexes['products_by_blueprint'].get((blueprint_type_id, '1'))
        activity_id = '1'

        if not products:
            # Check for reactions (activity 11)
            products = indexes['products_by_blueprint'].get((blueprint_type_id, '11'))
            activity_id = '11'

        if not products:
            return None, f"Error: Blueprint '{target_name}' has no manufacturing or reaction products"

        # Take the first product (typically there's only one)
        product = products[0]
        product_type_id = product['productTypeID']
        output_quantity = int(product['quantity'])

        # Get product info
        product_info = indexes['types_by_id'].get(product_type_id)
        if not product_info:
            return None, f"Error: Product type ID {product_type_id} not found"

        # Use product info for display
        display_info = product_info
        display_type_id = product_type_id

    else:
        # It's a product - find its blueprint
        product_type_id = type_id
        blueprint_type_id, activity_id = find_blueprint_for_product(product_type_id, indexes)

        if not blueprint_type_id:
            return None, f"Error: No blueprint found for '{target_name}'"

        # Get output quantity
        products = indexes['products_by_blueprint'].get((blueprint_type_id, activity_id))
        output_quantity = int(products[0]['quantity'])

        display_info = type_info
        display_type_id = type_id

    # Get materials
    materials = indexes['materials_by_blueprint'].get((blueprint_type_id, activity_id), [])

    # Get time (in seconds, convert to minutes, round up)
    time_seconds = indexes['time_by_blueprint'].get((blueprint_type_id, activity_id), 0)
    time_minutes = math.ceil(time_seconds / 60)

    # Get activity name
    activity_name = indexes['activity_names'].get(activity_id, 'Unknown')

    # Get group and category info
    group_id = display_info['groupID']
    group_info = indexes['groups_by_id'].get(group_id)
    category_info = None
    if group_info:
        category_id = group_info['categoryID']
        category_info = indexes['categories_by_id'].get(category_id)

    # Get market group path
    market_group_id = display_info.get('marketGroupID')
    market_group_path = get_market_group_path(market_group_id, indexes['market_groups_by_id'])

    # Get tech level
    tech_level = get_tech_level(display_type_id, indexes['meta_types_by_id'], indexes['meta_groups_by_id'])

    # Get volume
    volume = get_volume(display_info, group_info, category_info, indexes['ship_volumes'])

    # Build materials list with buildable status
    materials_list = []
    for m in materials:
        material_type_id = m['materialTypeID']
        material_quantity = int(m['quantity'])
        material_info = indexes['types_by_id'].get(material_type_id)
        if material_info:
            material_name = material_info['typeName']
            is_buildable = material_type_id in indexes['buildable_types']
            materials_list.append({
                'name': material_name,
                'quantity': material_quantity,
                'buildable': is_buildable,
            })

    # Sort materials alphabetically by name (case-insensitive)
    materials_list.sort(key=lambda x: x['name'].lower())

    return {
        'type_id': display_type_id,
        'type_name': display_info['typeName'],
        'category_name': category_info['categoryName'] if category_info else 'Unknown',
        'group_name': group_info['groupName'] if group_info else 'Unknown',
        'market_group_path': market_group_path,
        'tech_level': tech_level,
        'volume': volume,
        'activity_name': activity_name,
        'output_quantity': output_quantity,
        'run_time': time_minutes,
        'materials': materials_list,
    }, None


def format_recipe(recipe):
    """Format the recipe output."""
    lines = []

    # Item info
    lines.append(f"ITEM: {recipe['type_name']} ({recipe['type_id']})")

    # Group hierarchy
    lines.append(f"Group: {recipe['category_name']} > {recipe['group_name']}")

    # Market group
    market_group = recipe['market_group_path'] if recipe['market_group_path'] else 'None'
    lines.append(f"Market Group: {market_group}")

    # Tech level
    lines.append(f"Tech Level: {recipe['tech_level']}")

    # Volume (format with 2 decimal places if needed)
    if recipe['volume'] == int(recipe['volume']):
        lines.append(f"Volume: {int(recipe['volume'])}.00")
    else:
        lines.append(f"Volume: {recipe['volume']:.2f}")

    lines.append("")

    # Recipe section
    lines.append("Recipe:")
    lines.append(f"Activity: {recipe['activity_name']}")
    lines.append(f"Output Quantity: {recipe['output_quantity']}")
    lines.append(f"Run Time: {recipe['run_time']}")

    # Materials table
    lines.append("| Item | Quantity | Buildable |")
    lines.append("|:-:|:---:|---:|")

    for mat in recipe['materials']:
        buildable_str = 'Yes' if mat['buildable'] else 'No'
        lines.append(f"| {mat['name']} | {mat['quantity']} | {buildable_str} |")

    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(
        description='EVE Online Industry Recipe Planner'
    )
    subparsers = parser.add_subparsers(dest='command', help='Commands')

    # Recipe command
    recipe_parser = subparsers.add_parser('recipe', help='Get recipe for a product or blueprint')
    recipe_parser.add_argument('target', help='Product or Blueprint name (exact match)')
    recipe_parser.add_argument('--sde', required=True, help='Path to SDE directory')

    args = parser.parse_args()

    if args.command != 'recipe':
        parser.print_help()
        sys.exit(1)

    # Load SDE
    if not os.path.isdir(args.sde):
        print(f"Error: SDE directory '{args.sde}' not found", file=sys.stderr)
        sys.exit(1)

    try:
        sde = load_sde(args.sde)
        indexes = build_indexes(sde)
    except Exception as e:
        print(f"Error loading SDE: {e}", file=sys.stderr)
        sys.exit(1)

    # Get recipe
    recipe, error = get_recipe(args.target, indexes)

    if error:
        print(error, file=sys.stderr)
        sys.exit(1)

    # Output recipe
    print(format_recipe(recipe))


if __name__ == '__main__':
    main()
