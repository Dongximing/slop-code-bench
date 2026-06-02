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
    industry_activity_skills = load_bz2_csv(sde_path / 'industryActivitySkills.csv.bz2')
    industry_activity_probabilities = load_bz2_csv(sde_path / 'industryActivityProbabilities.csv.bz2')

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
    skills_by_blueprint = _group_by(industry_activity_skills, ('typeID', 'activityID'))
    probabilities_by_blueprint = _group_by(industry_activity_probabilities, ('typeID', 'activityID'))

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

    # productTypeID -> list of (blueprint_type_id, activity_id) for all blueprints
    product_to_all_blueprints = defaultdict(list)
    for (bp_id, act_id), products in products_by_blueprint.items():
        for p in products:
            product_to_all_blueprints[p['productTypeID']].append((bp_id, act_id))

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

# Tech III relic baselines (name -> (base_chance, base_runs))
RELEC_BASELINES = {
    'Intact':          (0.26, 20),
    'Malfunctioning':  (0.21, 10),
    'Wrecked':         (0.14, 3),
}

RELEC_QUALITY_ORDER = ['Intact', 'Malfunctioning', 'Wrecked']


def _compute_invention_probability(base_prob, skill_level, science_skill_count, prob_mult):
    """
    Compute invention probability using exact Decimal arithmetic.
    Formula:
      success = base * (1 + sum_science_levels/30 + encryption_level/40) * (1 + prob_mult/100)
    All skill levels are set to `skill_level`.
    Clamp to [0, 1].
    """
    base = Decimal(str(base_prob))
    sl = Decimal(str(skill_level))
    sc = Decimal(str(science_skill_count))
    pm = Decimal(str(prob_mult))

    science_sum = sc * sl
    skill_factor = Decimal(1) + science_sum / Decimal(30) + sl / Decimal(40)
    decryptor_factor = Decimal(1) + pm / Decimal(100)
    prob = base * skill_factor * decryptor_factor

    # Clamp
    if prob < 0:
        prob = Decimal(0)
    elif prob > 1:
        prob = Decimal(1)

    return float(prob)


def _round_pct(value):
    """Round a percentage to 2 decimal places using round-half-up."""
    return float(Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def _is_encryption_skill(skill_name):
    """Check if a skill name is an encryption methods skill."""
    return 'Encryption Methods' in skill_name


def _resolve_target_to_product(target_name, indexes):
    """
    Resolve a target name (product or blueprint) to the product type info.
    Returns (product_type_info, product_type_id) or (None, error_string).
    """
    type_info = indexes['types_by_name'].get(target_name)
    if not type_info:
        return None, f"Error: '{target_name}' not found in SDE"
    if type_info['published'] != '1':
        return None, f"Error: '{target_name}' is not published"

    if is_blueprint(type_info, indexes):
        # It's a blueprint name - find its manufacturing product
        bp_type_id = type_info['typeID']
        products = indexes['products_by_blueprint'].get((bp_type_id, '1'))
        if not products:
            products = indexes['products_by_blueprint'].get((bp_type_id, '11'))
        if not products:
            return None, f"Error: Blueprint '{target_name}' has no manufacturing products"
        product_type_id = products[0]['productTypeID']
        product_info = indexes['types_by_id'].get(product_type_id)
        if not product_info:
            return None, f"Error: Product type ID {product_type_id} not found"
        return product_info, None
    else:
        return type_info, None


def _find_invention_source_for_t2(product_type_id, indexes):
    """
    For a Tech II product, find the T1 blueprint that invents it.
    Returns (t1_blueprint_type_id, t2_blueprint_type_id, base_prob, base_runs)
    or (None, error).
    """
    # First find the T2 blueprint for this product
    t2_bp_result = indexes['product_to_blueprint'].get(product_type_id)
    if not t2_bp_result:
        return None, f"Error: No blueprint found manufacturing type {product_type_id}"

    t2_bp_type_id, mfg_act_id = t2_bp_result
    t2_bp_info = indexes['types_by_id'].get(t2_bp_type_id)
    if not t2_bp_info:
        return None, f"Error: T2 blueprint {t2_bp_type_id} not found"

    # Now find the T1 blueprint that invents this T2 blueprint via activity 8
    invention_sources = indexes['product_to_all_blueprints'].get(t2_bp_type_id, [])
    invention_bp_id = None
    for src_bp_id, act_id in invention_sources:
        if act_id == '8':
            invention_bp_id = src_bp_id
            break

    if invention_bp_id is None:
        return None, f"Error: No invention source found for {t2_bp_info['typeName']}"

    # Get base probability from probabilities table
    probs = indexes['probabilities_by_blueprint'].get((invention_bp_id, '8'), [])
    base_prob = None
    for p in probs:
        if p['productTypeID'] == t2_bp_type_id:
            base_prob = float(p['probability'])
            break

    if base_prob is None:
        return None, f"Error: No probability found for invention of {t2_bp_info['typeName']}"

    # Get base runs from products table (the quantity of the T2 blueprint produced)
    products = indexes['products_by_blueprint'].get((invention_bp_id, '8'), [])
    base_runs = None
    for p in products:
        if p['productTypeID'] == t2_bp_type_id:
            base_runs = int(p['quantity'])
            break

    if base_runs is None:
        return None, f"Error: No product quantity found for invention of {t2_bp_info['typeName']}"

    return (invention_bp_id, t2_bp_type_id, base_prob, base_runs), None


def _find_invention_sources_for_t3(product_type_id, indexes):
    """
    For a Tech III product, find all relics that invent its blueprint via activity 8.
    Returns list of (relic_type_id, relic_name, base_prob, base_runs)
    or (None, error).
    """
    # Find the T3 blueprint
    t3_bp_result = indexes['product_to_blueprint'].get(product_type_id)
    if not t3_bp_result:
        return None, f"Error: No blueprint found manufacturing type {product_type_id}"

    t3_bp_type_id, mfg_act_id = t3_bp_result

    # Find all invention sources (relics) for this blueprint
    invention_sources = indexes['product_to_all_blueprints'].get(t3_bp_type_id, [])
    relics = []
    for src_bp_id, act_id in invention_sources:
        if act_id == '8':
            relic_info = indexes['types_by_id'].get(src_bp_id)
            if not relic_info:
                continue
            relic_name = relic_info['typeName']

            # Get probability from probabilities table
            probs = indexes['probabilities_by_blueprint'].get((src_bp_id, '8'), [])
            base_prob = None
            for p in probs:
                if p['productTypeID'] == t3_bp_type_id:
                    base_prob = float(p['probability'])
                    break

            if base_prob is None:
                continue

            # Get base runs from products table
            products = indexes['products_by_blueprint'].get((src_bp_id, '8'), [])
            base_runs = None
            for p in products:
                if p['productTypeID'] == t3_bp_type_id:
                    base_runs = int(p['quantity'])
                    break

            if base_runs is None:
                continue

            relics.append((src_bp_id, relic_name, base_prob, base_runs))

    if not relics:
        return None, f"Error: No relic invention sources found"

    # Sort relics by quality (Intact > Malfunctioning > Wrecked)
    def relic_sort_key(r):
        name = r[1]
        for i, quality in enumerate(RELEC_QUALITY_ORDER):
            if name.startswith(quality):
                return i
        return len(RELEC_QUALITY_ORDER)

    relics.sort(key=relic_sort_key)
    return relics, None


def _get_skills_for_activity(blueprint_type_id, activity_id, indexes):
    """Get skill names for a blueprint activity, sorted alphabetically."""
    skills = indexes['skills_by_blueprint'].get((blueprint_type_id, activity_id), [])
    skill_names = []
    for s in skills:
        skill_info = indexes['types_by_id'].get(s['skillID'])
        if skill_info:
            skill_names.append(skill_info['typeName'])
    skill_names.sort(key=lambda x: x.lower())
    return skill_names


def _get_materials_for_activity(blueprint_type_id, activity_id, indexes):
    """Get datacore materials for a blueprint activity, sorted alphabetically."""
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


def _count_science_skills(skill_names):
    """Count non-encryption science skills."""
    return sum(1 for s in skill_names if not _is_encryption_skill(s))


def get_invention(target_name, indexes, skill_level=5):
    """Get invention information for a target product or blueprint."""
    product_info, error = _resolve_target_to_product(target_name, indexes)
    if error:
        return None, error

    product_type_id = product_info['typeID']
    tech_level = get_tech_level(
        product_type_id,
        indexes['meta_types_by_id'],
        indexes['meta_groups_by_id']
    )

    if tech_level not in ('Tech II', 'Tech III'):
        return None, f"Error: '{product_info['typeName']}' is {tech_level}, not Tech II or Tech III"

    group_info = indexes['groups_by_id'].get(product_info['groupID'])
    category_info = indexes['categories_by_id'].get(group_info['categoryID']) if group_info else None
    market_group_path = get_market_group_path(
        product_info.get('marketGroupID'), indexes['market_groups_by_id']
    )

    result = {
        'type_id': product_type_id,
        'type_name': product_info['typeName'],
        'category_name': category_info['categoryName'] if category_info else 'Unknown',
        'group_name': group_info['groupName'] if group_info else 'Unknown',
        'market_group_path': market_group_path,
        'tech_level': tech_level,
    }

    if tech_level == 'Tech II':
        invention_data, error = _find_invention_source_for_t2(product_type_id, indexes)
        if error:
            return None, error

        t1_bp_id, t2_bp_id, base_prob, base_runs = invention_data

        t1_bp_info = indexes['types_by_id'].get(t1_bp_id)
        result['base_bp_name'] = t1_bp_info['typeName']
        result['base_bp_id'] = t1_bp_id
        result['base_prob'] = base_prob
        result['base_runs'] = base_runs

        # Get skills, materials, time from the T1 blueprint activity 8
        skill_names = _get_skills_for_activity(t1_bp_id, '8', indexes)
        datacores = _get_materials_for_activity(t1_bp_id, '8', indexes)
        time_seconds = indexes['time_by_blueprint'].get((t1_bp_id, '8'), 0)
        time_minutes = math.ceil(time_seconds / 60)

        result['datacores'] = datacores
        result['required_per_run'] = datacores[0]['quantity'] if datacores else 0
        result['skills'] = skill_names
        result['time'] = time_minutes
        result['science_skill_count'] = _count_science_skills(skill_names)

        # Compute probabilities table
        prob_rows = []
        for dec in DECRYPTORS:
            prob = _compute_invention_probability(
                base_prob, skill_level, result['science_skill_count'], dec['prob_mult']
            )
            prob_pct = _round_pct(prob * 100)
            runs = base_runs + dec['runs']
            me = 2 + dec['me']
            te = 4 + dec['te']
            prob_rows.append({
                'decryptor': dec['name'],
                'probability': prob_pct,
                'runs': runs,
                'me': me,
                'te': te,
            })

        result['prob_rows'] = prob_rows

    else:  # Tech III
        relics, error = _find_invention_sources_for_t3(product_type_id, indexes)
        if error:
            return None, error

        # Use first relic for skills, materials, time (they're all the same)
        first_relic_id = relics[0][0]
        skill_names = _get_skills_for_activity(first_relic_id, '8', indexes)
        datacores = _get_materials_for_activity(first_relic_id, '8', indexes)
        time_seconds = indexes['time_by_blueprint'].get((first_relic_id, '8'), 0)
        time_minutes = math.ceil(time_seconds / 60)

        result['datacores'] = datacores
        result['required_per_run'] = datacores[0]['quantity'] if datacores else 0
        result['skills'] = skill_names
        result['time'] = time_minutes
        result['science_skill_count'] = _count_science_skills(skill_names)
        result['relics'] = relics

        # Compute probabilities table
        prob_rows = []
        for relic_id, relic_name, base_prob, base_runs in relics:
            for dec in DECRYPTORS:
                prob = _compute_invention_probability(
                    base_prob, skill_level, result['science_skill_count'], dec['prob_mult']
                )
                prob_pct = _round_pct(prob * 100)
                runs = base_runs + dec['runs']
                me = 2 + dec['me']
                te = 4 + dec['te']
                prob_rows.append({
                    'relic_name': relic_name,
                    'decryptor': dec['name'],
                    'probability': prob_pct,
                    'runs': runs,
                    'me': me,
                    'te': te,
                })

        result['prob_rows'] = prob_rows

    return result, None


def format_invention(inv):
    """Format the invention report as the canonical block."""
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
    else:  # Tech III
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
