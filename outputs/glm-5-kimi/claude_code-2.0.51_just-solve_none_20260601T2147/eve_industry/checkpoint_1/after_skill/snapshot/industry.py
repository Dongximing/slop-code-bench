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
from collections import defaultdict
from pathlib import Path

import yaml


def load_bz2_csv(filepath):
    with bz2.open(filepath, 'rt', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def load_ship_volumes(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f)
    return {k: v for k, v in data.items() if v is not None}


def _index_by(rows, key_field):
    return {row[key_field]: row for row in rows}


def _group_by(rows, key_fields):
    if isinstance(key_fields, str):
        key_fields = (key_fields,)
    else:
        key_fields = tuple(key_fields)
    result = defaultdict(list)
    for row in rows:
        key = tuple(row[f] for f in key_fields)
        result[key].append(row)
    return dict(result)


def build_indexes(sde_dir):
    sde_path = Path(sde_dir)

    inv_types = load_bz2_csv(sde_path / 'invTypes.csv.bz2')
    inv_groups = load_bz2_csv(sde_path / 'invGroups.csv.bz2')
    inv_categories = load_bz2_csv(sde_path / 'invCategories.csv.bz2')
    inv_market_groups = load_bz2_csv(sde_path / 'invMarketGroups.csv.bz2')
    inv_meta_types = load_bz2_csv(sde_path / 'invMetaTypes.csv.bz2')
    inv_meta_groups = load_bz2_csv(sde_path / 'invMetaGroups.csv.bz2')

    industry_activity_products = load_bz2_csv(sde_path / 'industryActivityProducts.csv.bz2')
    industry_activity_materials = load_bz2_csv(sde_path / 'industryActivityMaterials.csv.bz2')
    industry_activity = load_bz2_csv(sde_path / 'industryActivity.csv.bz2')
    ram_activities = load_bz2_csv(sde_path / 'ramActivities.csv.bz2')

    ship_volumes = load_ship_volumes(sde_path / 'ship_volumes.yaml')

    types_by_id = _index_by(inv_types, 'typeID')
    types_by_name = {t['typeName']: t for t in inv_types}
    groups_by_id = _index_by(inv_groups, 'groupID')
    categories_by_id = _index_by(inv_categories, 'categoryID')
    market_groups_by_id = _index_by(inv_market_groups, 'marketGroupID')
    meta_types_by_id = _index_by(inv_meta_types, 'typeID')
    meta_groups_by_id = _index_by(inv_meta_groups, 'metaGroupID')

    products_by_blueprint = _group_by(industry_activity_products, ('typeID', 'activityID'))
    materials_by_blueprint = _group_by(industry_activity_materials, ('typeID', 'activityID'))

    time_by_blueprint = {
        (a['typeID'], a['activityID']): int(a['time'])
        for a in industry_activity
    }
    activity_names = _index_by(ram_activities, 'activityID')
    activity_names = {aid: a['activityName'] for aid, a in activity_names.items()}

    # productTypeID -> (blueprint_type_id, activity_id) for reverse lookups
    product_to_blueprint = {}
    for (bp_id, act_id), products in products_by_blueprint.items():
        for p in products:
            product_to_blueprint[p['productTypeID']] = (bp_id, act_id)

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
        'product_to_blueprint': product_to_blueprint,
        'ship_volumes': ship_volumes,
        'buildable_types': buildable_types,
    }


def get_market_group_path(market_group_id, market_groups_by_id):
    if not market_group_id or market_group_id not in market_groups_by_id:
        return None

    path = []
    current_id = market_group_id
    while current_id and current_id in market_groups_by_id:
        mg = market_groups_by_id[current_id]
        path.append(mg['marketGroupName'])
        parent_id = mg['parentGroupID']
        if not parent_id or parent_id == 'None':
            break
        current_id = parent_id

    return ' > '.join(reversed(path))


_TECH_LEVELS = {'Tech I', 'Tech II', 'Tech III'}


def get_tech_level(type_id, meta_types_by_id, meta_groups_by_id):
    if type_id not in meta_types_by_id:
        return 'Tech I'
    meta_group_id = meta_types_by_id[type_id]['metaGroupID']
    if meta_group_id not in meta_groups_by_id:
        return 'Tech I'
    name = meta_groups_by_id[meta_group_id]['metaGroupName']
    return name if name in _TECH_LEVELS else 'Tech I'


def get_volume(type_info, group_info, category_info, ship_volumes):
    if category_info and category_info['categoryID'] == '6':
        group_name = group_info['groupName']
        if group_name in ship_volumes:
            return ship_volumes[group_name]

    volume_str = type_info.get('volume', '0')
    if volume_str and volume_str != 'None':
        try:
            return float(volume_str)
        except ValueError:
            return 0.0
    return 0.0


def is_blueprint(type_info, indexes):
    group_id = type_info['groupID']
    group_info = indexes['groups_by_id'].get(group_id)
    return group_info is not None and group_info['categoryID'] == '9'


def _resolve_blueprint_and_product(target_name, indexes):
    """Resolve a target name to (blueprint_type_id, activity_id, product_type_id, output_quantity, display_info)."""
    type_info = indexes['types_by_name'].get(target_name)
    if not type_info:
        return None, f"Error: '{target_name}' not found in SDE"
    if type_info['published'] != '1':
        return None, f"Error: '{target_name}' is not published"

    type_id = type_info['typeID']

    if is_blueprint(type_info, indexes):
        blueprint_type_id = type_id
        products = indexes['products_by_blueprint'].get((blueprint_type_id, '1'))
        activity_id = '1'
        if not products:
            products = indexes['products_by_blueprint'].get((blueprint_type_id, '11'))
            activity_id = '11'
        if not products:
            return None, f"Error: Blueprint '{target_name}' has no manufacturing or reaction products"

        product = products[0]
        product_type_id = product['productTypeID']
        output_quantity = int(product['quantity'])
        display_info = indexes['types_by_id'].get(product_type_id)
        if not display_info:
            return None, f"Error: Product type ID {product_type_id} not found"
    else:
        product_type_id = type_id
        result = indexes['product_to_blueprint'].get(product_type_id)
        if not result:
            return None, f"Error: No blueprint found for '{target_name}'"
        blueprint_type_id, activity_id = result
        products = indexes['products_by_blueprint'].get((blueprint_type_id, activity_id))
        output_quantity = int(products[0]['quantity'])
        display_info = type_info

    return (blueprint_type_id, activity_id, display_info, output_quantity), None


def get_recipe(target_name, indexes):
    resolved, error = _resolve_blueprint_and_product(target_name, indexes)
    if error:
        return None, error

    blueprint_type_id, activity_id, display_info, output_quantity = resolved
    display_type_id = display_info['typeID']

    materials = indexes['materials_by_blueprint'].get((blueprint_type_id, activity_id), [])
    time_seconds = indexes['time_by_blueprint'].get((blueprint_type_id, activity_id), 0)
    time_minutes = math.ceil(time_seconds / 60)
    activity_name = indexes['activity_names'].get(activity_id, 'Unknown')

    group_info = indexes['groups_by_id'].get(display_info['groupID'])
    category_info = indexes['categories_by_id'].get(group_info['categoryID']) if group_info else None
    market_group_path = get_market_group_path(
        display_info.get('marketGroupID'), indexes['market_groups_by_id']
    )
    tech_level = get_tech_level(display_type_id, indexes['meta_types_by_id'], indexes['meta_groups_by_id'])
    volume = get_volume(display_info, group_info, category_info, indexes['ship_volumes'])

    materials_list = []
    for m in materials:
        material_info = indexes['types_by_id'].get(m['materialTypeID'])
        if material_info:
            materials_list.append({
                'name': material_info['typeName'],
                'quantity': int(m['quantity']),
                'buildable': m['materialTypeID'] in indexes['buildable_types'],
            })
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
    lines = []
    lines.append(f"ITEM: {recipe['type_name']} ({recipe['type_id']})")
    lines.append(f"Group: {recipe['category_name']} > {recipe['group_name']}")
    lines.append(f"Market Group: {recipe['market_group_path'] or 'None'}")
    lines.append(f"Tech Level: {recipe['tech_level']}")
    if recipe['volume'] == int(recipe['volume']):
        lines.append(f"Volume: {int(recipe['volume'])}.00")
    else:
        lines.append(f"Volume: {recipe['volume']:.2f}")
    lines.append("")
    lines.append("Recipe:")
    lines.append(f"Activity: {recipe['activity_name']}")
    lines.append(f"Output Quantity: {recipe['output_quantity']}")
    lines.append(f"Run Time: {recipe['run_time']}")
    lines.append("| Item | Quantity | Buildable |")
    lines.append("|:-:|:---:|---:|")
    for mat in recipe['materials']:
        buildable_str = 'Yes' if mat['buildable'] else 'No'
        lines.append(f"| {mat['name']} | {mat['quantity']} | {buildable_str} |")
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description='EVE Online Industry Recipe Planner')
    subparsers = parser.add_subparsers(dest='command')

    recipe_parser = subparsers.add_parser('recipe', help='Get recipe for a product or blueprint')
    recipe_parser.add_argument('target', help='Product or Blueprint name (exact match)')
    recipe_parser.add_argument('--sde', required=True, help='Path to SDE directory')

    args = parser.parse_args()

    if args.command != 'recipe':
        parser.print_help()
        sys.exit(1)

    if not os.path.isdir(args.sde):
        print(f"Error: SDE directory '{args.sde}' not found", file=sys.stderr)
        sys.exit(1)

    try:
        indexes = build_indexes(args.sde)
    except Exception as e:
        print(f"Error loading SDE: {e}", file=sys.stderr)
        sys.exit(1)

    recipe, error = get_recipe(args.target, indexes)
    if error:
        print(error, file=sys.stderr)
        sys.exit(1)

    print(format_recipe(recipe))


if __name__ == '__main__':
    main()
