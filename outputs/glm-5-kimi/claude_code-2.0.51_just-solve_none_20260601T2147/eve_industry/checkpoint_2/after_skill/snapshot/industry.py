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
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import yaml


def load_bz2_csv(filepath):
    with bz2.open(filepath, 'rt', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def load_ship_volumes(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f)
    return {k: v for k, v in data.items() if v is not None}


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
    industry_activity_skills = load_bz2_csv(sde_path / 'industryActivitySkills.csv.bz2')
    industry_activity_probabilities = load_bz2_csv(sde_path / 'industryActivityProbabilities.csv.bz2')

    ship_volumes = load_ship_volumes(sde_path / 'ship_volumes.yaml')

    types_by_id = {row['typeID']: row for row in inv_types}
    types_by_name = {t['typeName']: t for t in inv_types}
    groups_by_id = {row['groupID']: row for row in inv_groups}
    categories_by_id = {row['categoryID']: row for row in inv_categories}
    market_groups_by_id = {row['marketGroupID']: row for row in inv_market_groups}
    meta_types_by_id = {row['typeID']: row for row in inv_meta_types}
    meta_groups_by_id = {row['metaGroupID']: row for row in inv_meta_groups}

    products_by_blueprint = _group_by(industry_activity_products, ('typeID', 'activityID'))
    materials_by_blueprint = _group_by(industry_activity_materials, ('typeID', 'activityID'))
    skills_by_blueprint = _group_by(industry_activity_skills, ('typeID', 'activityID'))
    probabilities_by_blueprint = _group_by(industry_activity_probabilities, ('typeID', 'activityID'))

    time_by_blueprint = {
        (a['typeID'], a['activityID']): int(a['time'])
        for a in industry_activity
    }
    activity_names = {a['activityID']: a['activityName'] for a in ram_activities}

    product_to_blueprint = {}
    product_to_all_blueprints = defaultdict(list)
    buildable_types = set()
    for (bp_id, act_id), products in products_by_blueprint.items():
        for p in products:
            pid = p['productTypeID']
            product_to_blueprint[pid] = (bp_id, act_id)
            product_to_all_blueprints[pid].append((bp_id, act_id))
            buildable_types.add(pid)

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
        'skills_by_blueprint': skills_by_blueprint,
        'probabilities_by_blueprint': probabilities_by_blueprint,
        'time_by_blueprint': time_by_blueprint,
        'activity_names': activity_names,
        'product_to_blueprint': product_to_blueprint,
        'product_to_all_blueprints': dict(product_to_all_blueprints),
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


def _resolve_target(target_name, indexes):
    """Resolve a target name to product/blueprint info.
    Returns (resolved_dict, None) on success or (None, error_string) on failure.
    The resolved dict contains: type_info, type_id, blueprint_type_id, activity_id,
    product_type_id, output_quantity, display_info.
    """
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

    return {
        'type_info': type_info,
        'type_id': type_id,
        'blueprint_type_id': blueprint_type_id,
        'activity_id': activity_id,
        'product_type_id': product_type_id,
        'output_quantity': output_quantity,
        'display_info': display_info,
    }, None


def get_recipe(target_name, indexes):
    resolved, error = _resolve_target(target_name, indexes)
    if error:
        return None, error

    bp_id = resolved['blueprint_type_id']
    activity_id = resolved['activity_id']
    display_info = resolved['display_info']
    display_type_id = display_info['typeID']

    materials = indexes['materials_by_blueprint'].get((bp_id, activity_id), [])
    time_seconds = indexes['time_by_blueprint'].get((bp_id, activity_id), 0)
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
        'output_quantity': resolved['output_quantity'],
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


# ──────────────────────────────────────────────────────────────────────
# Decryptor definitions
# ──────────────────────────────────────────────────────────────────────

DECRYPTORS = [
    {'name': 'None',                            'prob_mult': 0,   'runs': 0,  'me': 0,  'te': 0},
    {'name': 'Accelerant Decryptor',            'prob_mult': 20,  'runs': 1,  'me': 2,  'te': 10},
    {'name': 'Attainment Decryptor',            'prob_mult': 80,  'runs': 4,  'me': -1, 'te': 4},
    {'name': 'Augmentation Decryptor',          'prob_mult': -40, 'runs': 9,  'me': -2, 'te': 2},
    {'name': 'Optimized Attainment Decryptor',  'prob_mult': 90,  'runs': 2,  'me': 1,  'te': -2},
    {'name': 'Optimized Augmentation Decryptor','prob_mult': -10, 'runs': 7,  'me': 2,  'te': 0},
    {'name': 'Parity Decryptor',                'prob_mult': 50,  'runs': 3,  'me': 1,  'te': -2},
    {'name': 'Process Decryptor',               'prob_mult': 10,  'runs': 0,  'me': 3,  'te': 6},
    {'name': 'Symmetry Decryptor',              'prob_mult': 0,   'runs': 2,  'me': 1,  'te': 8},
]

RELEC_QUALITY_ORDER = ['Intact', 'Malfunctioning', 'Wrecked']


def _compute_invention_probability(base_prob, skill_level, science_skill_count, prob_mult):
    base = Decimal(str(base_prob))
    sl = Decimal(str(skill_level))
    sc = Decimal(str(science_skill_count))
    pm = Decimal(str(prob_mult))

    science_sum = sc * sl
    skill_factor = Decimal(1) + science_sum / Decimal(30) + sl / Decimal(40)
    decryptor_factor = Decimal(1) + pm / Decimal(100)
    prob = base * skill_factor * decryptor_factor

    return float(max(Decimal(0), min(Decimal(1), prob)))


def _round_pct(value):
    return float(Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def _find_invention_sources(product_type_id, indexes):
    """Find all invention sources for a product via activity 8.
    Returns list of (source_bp_id, source_name, base_prob, base_runs) or (None, error).
    """
    bp_result = indexes['product_to_blueprint'].get(product_type_id)
    if not bp_result:
        return None, f"Error: No blueprint found manufacturing type {product_type_id}"

    bp_type_id, _ = bp_result
    invention_sources = indexes['product_to_all_blueprints'].get(bp_type_id, [])

    results = []
    for src_bp_id, act_id in invention_sources:
        if act_id != '8':
            continue

        src_info = indexes['types_by_id'].get(src_bp_id)
        if not src_info:
            continue

        probs = indexes['probabilities_by_blueprint'].get((src_bp_id, '8'), [])
        base_prob = None
        for p in probs:
            if p['productTypeID'] == bp_type_id:
                base_prob = float(p['probability'])
                break

        if base_prob is None:
            continue

        products = indexes['products_by_blueprint'].get((src_bp_id, '8'), [])
        base_runs = None
        for p in products:
            if p['productTypeID'] == bp_type_id:
                base_runs = int(p['quantity'])
                break

        if base_runs is None:
            continue

        results.append((src_bp_id, src_info['typeName'], base_prob, base_runs))

    if not results:
        return None, f"Error: No invention source found for type {product_type_id}"

    return results, None


def _get_skills_for_activity(blueprint_type_id, activity_id, indexes):
    skills = indexes['skills_by_blueprint'].get((blueprint_type_id, activity_id), [])
    skill_names = []
    for s in skills:
        skill_info = indexes['types_by_id'].get(s['skillID'])
        if skill_info:
            skill_names.append(skill_info['typeName'])
    skill_names.sort(key=lambda x: x.lower())
    return skill_names


def _get_materials_for_activity(blueprint_type_id, activity_id, indexes):
    materials = indexes['materials_by_blueprint'].get((blueprint_type_id, activity_id), [])
    datacores = []
    for m in materials:
        mat_info = indexes['types_by_id'].get(m['materialTypeID'])
        if mat_info:
            datacores.append({
                'name': mat_info['typeName'],
                'quantity': int(m['quantity']),
            })
    datacores.sort(key=lambda x: x['name'].lower())
    return datacores


def _build_prob_rows(sources, science_skill_count, skill_level, is_t3=False):
    """Build probability table rows from invention sources and decryptors."""
    prob_rows = []
    if not is_t3:
        # T2: single source
        _, _, base_prob, base_runs = sources[0]
        for dec in DECRYPTORS:
            prob = _compute_invention_probability(
                base_prob, skill_level, science_skill_count, dec['prob_mult']
            )
            prob_rows.append({
                'decryptor': dec['name'],
                'probability': _round_pct(prob * 100),
                'runs': base_runs + dec['runs'],
                'me': 2 + dec['me'],
                'te': 4 + dec['te'],
            })
    else:
        for relic_id, relic_name, base_prob, base_runs in sources:
            for dec in DECRYPTORS:
                prob = _compute_invention_probability(
                    base_prob, skill_level, science_skill_count, dec['prob_mult']
                )
                prob_rows.append({
                    'relic_name': relic_name,
                    'decryptor': dec['name'],
                    'probability': _round_pct(prob * 100),
                    'runs': base_runs + dec['runs'],
                    'me': 2 + dec['me'],
                    'te': 4 + dec['te'],
                })
    return prob_rows


def get_invention(target_name, indexes, skill_level=5):
    resolved, error = _resolve_target(target_name, indexes)
    if error:
        return None, error

    product_type_id = resolved['product_type_id']
    tech_level = get_tech_level(
        product_type_id,
        indexes['meta_types_by_id'],
        indexes['meta_groups_by_id']
    )

    if tech_level not in ('Tech II', 'Tech III'):
        return None, f"Error: '{resolved['display_info']['typeName']}' is {tech_level}, not Tech II or Tech III"

    display_info = resolved['display_info']
    group_info = indexes['groups_by_id'].get(display_info['groupID'])
    category_info = indexes['categories_by_id'].get(group_info['categoryID']) if group_info else None
    market_group_path = get_market_group_path(
        display_info.get('marketGroupID'), indexes['market_groups_by_id']
    )

    is_t3 = tech_level == 'Tech III'

    sources, error = _find_invention_sources(product_type_id, indexes)
    if error:
        return None, error

    # Sort T3 relics by quality
    if is_t3:
        def relic_sort_key(r):
            for i, quality in enumerate(RELEC_QUALITY_ORDER):
                if r[1].startswith(quality):
                    return i
            return len(RELEC_QUALITY_ORDER)
        sources.sort(key=relic_sort_key)

    first_source_bp_id = sources[0][0]
    skill_names = _get_skills_for_activity(first_source_bp_id, '8', indexes)
    datacores = _get_materials_for_activity(first_source_bp_id, '8', indexes)
    time_seconds = indexes['time_by_blueprint'].get((first_source_bp_id, '8'), 0)
    science_skill_count = sum(1 for s in skill_names if 'Encryption Methods' not in s)

    result = {
        'type_id': product_type_id,
        'type_name': display_info['typeName'],
        'category_name': category_info['categoryName'] if category_info else 'Unknown',
        'group_name': group_info['groupName'] if group_info else 'Unknown',
        'market_group_path': market_group_path,
        'tech_level': tech_level,
        'datacores': datacores,
        'required_per_run': datacores[0]['quantity'] if datacores else 0,
        'skills': skill_names,
        'time': math.ceil(time_seconds / 60),
        'science_skill_count': science_skill_count,
        'prob_rows': _build_prob_rows(sources, science_skill_count, skill_level, is_t3),
    }

    if not is_t3:
        result['base_bp_name'] = indexes['types_by_id'][sources[0][0]]['typeName']
        result['base_bp_id'] = sources[0][0]
        result['base_prob'] = sources[0][2]
        result['base_runs'] = sources[0][3]
    else:
        result['relics'] = sources

    return result, None


def format_invention(inv):
    lines = []

    lines.append(f"ITEM: {inv['type_name']} ({inv['type_id']})")
    lines.append(f"Group: {inv['category_name']} > {inv['group_name']}")
    lines.append(f"Market Group: {inv['market_group_path'] or 'None'}")
    lines.append(f"Tech Level: {inv['tech_level']}")
    lines.append("")
    lines.append("Invention:")

    if inv['tech_level'] == 'Tech II':
        lines.append(f"Base: {inv['base_bp_name']} ({inv['base_bp_id']})")

    datacore_names = ', '.join(d['name'] for d in inv['datacores'])
    lines.append(f"Datacores: {datacore_names}")
    lines.append(f"Required Per Run: {inv['required_per_run']}")
    lines.append(f"Skills: {', '.join(inv['skills'])}")
    lines.append(f"Time: {inv['time']}")

    lines.append("Probabilities:")
    if inv['tech_level'] == 'Tech II':
        lines.append("| Decryptor                       | Probability | Runs | ME | TE |")
        lines.append("|---------------------------------|-------------|------|----|----|")
        for row in inv['prob_rows']:
            dname = row['decryptor']
            lines.append(
                f"| {dname:<32}| {row['probability']:<11.2f} | {row['runs']:<4} | {row['me']:<2} | {row['te']:<2} |"
            )
    else:
        lines.append("| Invented From                | Decryptor                       | Probability | Runs | ME | TE |")
        lines.append("|------------------------------|---------------------------------|-------------|------|----|----|")
        for row in inv['prob_rows']:
            relic = row['relic_name']
            dname = row['decryptor']
            lines.append(
                f"| {relic:<28} | {dname:<32}| {row['probability']:<11.2f} | {row['runs']:<4} | {row['me']:<2} | {row['te']:<2} |"
            )

    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description='EVE Online Industry Recipe Planner')
    subparsers = parser.add_subparsers(dest='command')

    recipe_parser = subparsers.add_parser('recipe', help='Get recipe for a product or blueprint')
    recipe_parser.add_argument('target', help='Product or Blueprint name (exact match)')
    recipe_parser.add_argument('--sde', required=True, help='Path to SDE directory')

    invention_parser = subparsers.add_parser('invention', help='Get invention report for a Tech II/III product or blueprint')
    invention_parser.add_argument('target', help='Product or Blueprint name (exact match)')
    invention_parser.add_argument('--sde', required=True, help='Path to SDE directory')
    invention_parser.add_argument('--skill', type=int, default=5, choices=[1, 2, 3, 4, 5],
                                  help='Skill level for all relevant skills (default: 5)')

    args = parser.parse_args()

    if args.command not in ('recipe', 'invention'):
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

    if args.command == 'recipe':
        recipe, error = get_recipe(args.target, indexes)
        if error:
            print(error, file=sys.stderr)
            sys.exit(1)
        print(format_recipe(recipe))
    elif args.command == 'invention':
        inv, error = get_invention(args.target, indexes, args.skill)
        if error:
            print(error, file=sys.stderr)
            sys.exit(1)
        print(format_invention(inv))


if __name__ == '__main__':
    main()
