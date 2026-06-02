#!/usr/bin/env python3
"""EVE Online Industrial Planner - Parse SDE and emit recipe reports."""

import argparse
import bz2
import csv
import math
import sys
from pathlib import Path
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

import yaml


def load_csv_bz2(filepath: Path):
    """Load CSV from bzip2 compressed file."""
    with bz2.open(filepath, 'rt', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def load_yaml_file(filepath: Path):
    """Load YAML file."""
    with open(filepath, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


@dataclass
class SDEData:
    """Parsed Static Data Export with indices for fast lookups."""
    types: dict[int, dict] = field(default_factory=dict)
    groups: dict[int, dict] = field(default_factory=dict)
    categories: dict[int, dict] = field(default_factory=dict)
    market_groups: dict[int, dict] = field(default_factory=dict)
    meta_types: dict[int, int] = field(default_factory=dict)
    meta_groups: dict[int, str] = field(default_factory=dict)
    activities: dict[int, dict[int, int]] = field(default_factory=lambda: defaultdict(dict))
    products: dict[int, dict[int, dict[int, int]]] = field(default_factory=lambda: defaultdict(lambda: defaultdict(dict)))
    materials: dict[int, dict[int, list]] = field(default_factory=lambda: defaultdict(lambda: defaultdict(list)))
    probabilities: dict[int, dict[int, dict[int, float]]] = field(default_factory=lambda: defaultdict(lambda: defaultdict(dict)))
    skills: dict[int, dict[int, list]] = field(default_factory=lambda: defaultdict(lambda: defaultdict(list)))
    activity_names: dict[int, str] = field(default_factory=dict)
    ship_volumes: dict[str, float] = field(default_factory=dict)
    buildable_types: set[int] = field(default_factory=set)
    # Reverse lookup: product_id -> (blueprint_id, activity_id)
    inventable_by_product: dict[int, tuple[int, int]] = field(default_factory=dict)
    # Reverse lookup: blueprint_name -> blueprint_id
    blueprint_names: dict[str, int] = field(default_factory=dict)


def load_type_row(row):
    """Process a single invTypes row, return None if not published."""
    if int(row.get('published', 0)) != 1:
        return None
    return {
        'typeID': int(row['typeID']),
        'groupID': int(row['groupID']),
        'typeName': row['typeName'],
        'volume': row.get('volume', '0'),
        'marketGroupID': int(row['marketGroupID']) if row.get('marketGroupID') and row['marketGroupID'] != 'None' else None,
        'mass': row.get('mass', '0'),
        'raceID': row.get('raceID'),
        'basePrice': row.get('basePrice'),
        'description': row.get('description', ''),
    }


def load_group_row(row):
    """Process a single invGroups row."""
    if int(row.get('published', 0)) != 1:
        return None
    return {
        'groupID': int(row['groupID']),
        'categoryID': int(row['categoryID']),
        'groupName': row['groupName'],
        'iconID': row.get('iconID'),
    }


def load_category_row(row):
    """Process a single invCategories row."""
    if int(row.get('published', 0)) != 1:
        return None
    return {
        'categoryID': int(row['categoryID']),
        'categoryName': row['categoryName'],
        'iconID': row.get('iconID'),
    }


def load_market_group_row(row):
    """Process a single invMarketGroups row."""
    mg_id = int(row['marketGroupID'])
    parent_id = int(row['parentGroupID']) if row.get('parentGroupID') and row['parentGroupID'] != 'None' else None
    return mg_id, {
        'marketGroupID': mg_id,
        'parentGroupID': parent_id,
        'marketGroupName': row['marketGroupName'],
        'description': row.get('description', ''),
        'iconID': row.get('iconID'),
        'hasTypes': int(row.get('hasTypes', 0)) == 1,
    }


def parse_sde(sde_dir: Path) -> SDEData:
    """Parse all SDE files and return structured data."""
    data = SDEData()

    # Load types with published filter
    for row in load_csv_bz2(sde_dir / 'invTypes.csv.bz2'):
        t = load_type_row(row)
        if t:
            data.types[t['typeID']] = t

    # Load groups
    for row in load_csv_bz2(sde_dir / 'invGroups.csv.bz2'):
        g = load_group_row(row)
        if g:
            data.groups[g['groupID']] = g

    # Load categories
    for row in load_csv_bz2(sde_dir / 'invCategories.csv.bz2'):
        c = load_category_row(row)
        if c:
            data.categories[c['categoryID']] = c

    # Load market groups
    for row in load_csv_bz2(sde_dir / 'invMarketGroups.csv.bz2'):
        mg_id, mg_data = load_market_group_row(row)
        data.market_groups[mg_id] = mg_data

    # Load meta types
    for row in load_csv_bz2(sde_dir / 'invMetaTypes.csv.bz2'):
        if row.get('metaGroupID'):
            data.meta_types[int(row['typeID'])] = int(row['metaGroupID'])

    # Load meta groups
    for row in load_csv_bz2(sde_dir / 'invMetaGroups.csv.bz2'):
        data.meta_groups[int(row['metaGroupID'])] = row['metaGroupName']

    # Load activities
    for row in load_csv_bz2(sde_dir / 'industryActivity.csv.bz2'):
        type_id = int(row['typeID'])
        activity_id = int(row['activityID'])
        data.activities[type_id][activity_id] = int(row['time'])

    # Load products
    for row in load_csv_bz2(sde_dir / 'industryActivityProducts.csv.bz2'):
        type_id = int(row['typeID'])
        activity_id = int(row['activityID'])
        product_id = int(row['productTypeID'])
        quantity = int(row['quantity'])
        data.products[type_id][activity_id][product_id] = quantity

    # Load materials
    for row in load_csv_bz2(sde_dir / 'industryActivityMaterials.csv.bz2'):
        type_id = int(row['typeID'])
        activity_id = int(row['activityID'])
        material = {'typeID': int(row['materialTypeID']), 'quantity': int(row['quantity'])}
        data.materials[type_id][activity_id].append(material)

    # Load probabilities
    for row in load_csv_bz2(sde_dir / 'industryActivityProbabilities.csv.bz2'):
        type_id = int(row['typeID'])
        activity_id = int(row['activityID'])
        product_id = int(row['productTypeID'])
        probability = float(row['probability'])
        data.probabilities[type_id][activity_id][product_id] = probability

    # Load skills
    for row in load_csv_bz2(sde_dir / 'industryActivitySkills.csv.bz2'):
        type_id = int(row['typeID'])
        activity_id = int(row['activityID'])
        skill = {'skillID': int(row['skillID']), 'level': int(row['level'])}
        data.skills[type_id][activity_id].append(skill)

    # Load activity names
    for row in load_csv_bz2(sde_dir / 'ramActivities.csv.bz2'):
        if row.get('published') == '1':
            data.activity_names[int(row['activityID'])] = row['activityName']

    # Load ship volumes
    data.ship_volumes = {k: float(v) for k, v in load_yaml_file(sde_dir / 'ship_volumes.yaml').items()}

    # Build reverse indices
    for type_id, activities in data.products.items():
        if 1 in activities or 8 in activities:
            data.buildable_types.add(type_id)

        # Track inventable products
        if 8 in activities:
            for product_id in activities[8]:
                data.inventable_by_product[product_id] = (type_id, 8)

    # Build blueprint name lookup for published types
    for type_id, type_data in data.types.items():
        if 'Blueprint' in type_data['typeName']:
            data.blueprint_names[type_data['typeName']] = type_id

    return data


# Activity constants
INVENTION = 8
MANUFACTURING = 1


def get_type_name(data: SDEData, type_id: int) -> str:
    """Get type name by ID, returns empty string if not found."""
    return data.types.get(type_id, {}).get('typeName', '')


def get_group_path(data: SDEData, type_id: int):
    """Return (category_name, group_name) tuple."""
    type_data = data.types.get(type_id)
    if not type_data:
        return 'Unknown', 'Unknown'
    group_data = data.groups.get(type_data['groupID'])
    if not group_data:
        return 'Unknown', 'Unknown'
    cat_data = data.categories.get(group_data['categoryID'])
    if cat_data:
        return cat_data['categoryName'], group_data['groupName']
    return 'Unknown', group_data['groupName']


def get_market_group_path(data: SDEData, type_id: int):
    """Return market group path as ' > '.joined string or None."""
    type_data = data.types.get(type_id)
    if not type_data or not type_data.get('marketGroupID'):
        return None
    path_parts = []
    mg_id = type_data['marketGroupID']
    while mg_id is not None:
        mg_data = data.market_groups.get(mg_id)
        if not mg_data:
            break
        path_parts.append(mg_data['marketGroupName'])
        mg_id = mg_data.get('parentGroupID')
    return ' > '.join(reversed(path_parts)) if path_parts else None


def get_tech_level(data: SDEData, type_id: int) -> str:
    """Determine tech level from meta groups."""
    meta_group_id = data.meta_types.get(type_id)
    if not meta_group_id:
        return 'Tech I'
    meta_name = data.meta_groups.get(meta_group_id, '')
    if meta_name in ('Tech II', 'Tech III'):
        return meta_name
    return 'Tech I'


def get_volume(data: SDEData, type_id: int) -> float:
    """Get volume with ship override."""
    type_data = data.types.get(type_id)
    if not type_data:
        return 0.0
    _, group_name = get_group_path(data, type_id)
    if group_name in data.ship_volumes:
        return data.ship_volumes[group_name]
    try:
        return float(type_data.get('volume') or '0')
    except ValueError:
        return 0.0


def get_invention_data(data: SDEData, product_id: int, skill_level: int) -> Optional[dict]:
    """Get invention data for a Tech II or Tech III product."""
    blueprint_id, activity_id = data.inventable_by_product.get(product_id, (None, None))
    if not blueprint_id:
        return None

    # Get invention time
    time_minutes = math.ceil(data.activities.get(blueprint_id, {}).get(INVENTION, 0) / 60)

    # Get materials (filter for datacores)
    datacore_materials = data.materials.get(blueprint_id, {}).get(INVENTION, [])
    datacores = []
    for mat in datacore_materials:
        mat_name = get_type_name(data, mat['typeID'])
        if mat_name and mat_name.startswith('Datacore - '):
            datacores.append({
                'typeID': mat['typeID'],
                'name': mat_name,
                'quantity': mat['quantity']
            })

    # Get skills
    skill_list = data.skills.get(blueprint_id, {}).get(INVENTION, [])
    skills = [{'name': get_type_name(data, skill['skillID']), 'required_level': skill['level']} for skill in skill_list]

    # Get base probability
    base_prob = data.probabilities.get(blueprint_id, {}).get(INVENTION, {}).get(product_id, 0.0)

    return {
        'invention_blueprint_id': blueprint_id,
        'activity_id': activity_id,
        'time_minutes': time_minutes,
        'datacores': datacores,
        'skills': skills,
        'base_probability': base_prob,
        'tech_level': get_tech_level(data, product_id)
    }


def get_recipe(data: SDEData, type_id: int, material_efficiency: int = 0) -> Optional[dict]:
    """Get manufacturing recipe for a type. Returns None if not manufacturable."""
    activities = data.products.get(type_id, {})
    if MANUFACTURING not in activities:
        return None
    time_raw = data.activities.get(type_id, {}).get(MANUFACTURING, 0)
    materials = data.materials.get(type_id, {}).get(MANUFACTURING, [])

    if material_efficiency > 0:
        adjusted_materials = []
        for mat in materials:
            base_qty = mat['quantity']
            adjusted_qty = math.ceil(max(1, base_qty * (1 - material_efficiency / 100)))
            adjusted_materials.append({
                'typeID': mat['typeID'],
                'quantity': adjusted_qty,
                'original_quantity': base_qty
            })
        materials = adjusted_materials
    else:
        materials = [{'typeID': m['typeID'], 'quantity': m['quantity'], 'original_quantity': m['quantity']} for m in materials]

    return {
        'activity': data.activity_names.get(MANUFACTURING, 'Manufacturing'),
        'output_qty': sum(activities[MANUFACTURING].values()),
        'run_time': round(time_raw / 60),
        'materials': materials
    }


def is_buildable(data: SDEData, type_id: int) -> bool:
    """Check if a type can be manufactured or invented."""
    return type_id in data.buildable_types


# Decryptor modifiers: (probability_multiplier_percent, runs, me, te)
DECRYPTORS = {
    'None': (0, 0, 0, 0),
    'Accelerant Decryptor': (20, 1, 2, 10),
    'Attainment Decryptor': (80, 4, -1, 4),
    'Augmentation Decryptor': (-40, 9, -2, 2),
    'Optimized Attainment Decryptor': (90, 2, 1, -2),
    'Optimized Augmentation Decryptor': (-10, 7, 2, 0),
    'Parity Decryptor': (50, 3, 1, -2),
    'Process Decryptor': (10, 0, 3, 6),
    'Symmetry Decryptor': (0, 2, 1, 8),
}

# Tech III relic baselines
TECHIII_RELICS = {
    'Intact': 0.26,
    'Malfunctioning': 0.21,
    'Wrecked': 0.14,
}
TECHIII_BASE_RUNS = {
    'Intact': 20,
    'Malfunctioning': 10,
    'Wrecked': 3,
}


def calculate_probability(base_prob: float, skill_level: int, decryptor_name: str) -> float:
    """Calculate success probability with skills and decryptor."""
    base_with_skills = base_prob * (1 + 7 * skill_level / 120)
    decryptor = DECRYPTORS.get(decryptor_name, (0, 0, 0, 0))
    prob_mult = decryptor[0] / 100.0
    return max(0.0, min(1.0, base_with_skills * (1 + prob_mult)))


def find_product_by_name(data: SDEData, name: str, tech_levels: tuple[str, ...] = ('Tech II', 'Tech III')):
    """Find type data for product name or blueprint name. Returns (type_data, type_id, is_blueprint_lookup) or (None, None, False)."""
    # Try direct lookup
    for type_id, type_data in data.types.items():
        if type_data['typeName'] == name:
            return type_data, type_id, False

    # Try blueprint name lookup
    for bp_name, bp_id in data.blueprint_names.items():
        if bp_name == name:
            return data.types.get(bp_id), bp_id, True

    return None, None, False


def generate_invention_report(data: SDEData, lookup_name: str, skill_level: int) -> str:
    """Generate invention report for a Tech II or Tech III product."""
    type_data, type_id, is_blueprint = find_product_by_name(data, lookup_name)
    if not type_data:
        return f"Error: Item '{lookup_name}' not found or not published.\n"

    # If blueprint, find the product it invents
    if is_blueprint:
        inv_products = data.products.get(type_id, {}).get(INVENTION, {})
        if inv_products:
            product_id = next(iter(inv_products.keys()))
            type_id = product_id
            type_data = data.types.get(product_id)
        else:
            return f"Error: '{lookup_name}' is not an inventable blueprint.\n"

    inv_data = get_invention_data(data, type_id, skill_level)
    if not inv_data:
        return f"Error: '{lookup_name}' is not an inventable item.\n"

    tech_level = inv_data['tech_level']
    if tech_level not in ('Tech II', 'Tech III'):
        return f"Error: '{lookup_name}' is not Tech II or Tech III.\n"

    cat_name, group_name = get_group_path(data, type_id)
    mg_path = get_market_group_path(data, type_id)

    lines = [
        f"ITEM: {type_data['typeName']} ({type_id})",
        f"Group: {cat_name} > {group_name}",
        f"Market Group: {mg_path if mg_path else 'None'}",
        f"Tech Level: {tech_level}",
        "",
        "Invention:",
    ]

    if tech_level == 'Tech II':
        lines.append(f"Base: {get_type_name(data, inv_data['invention_blueprint_id'])} ({inv_data['invention_blueprint_id']})")

    # Datacores
    datacores = sorted(inv_data['datacores'], key=lambda d: d['name'].lower())
    lines.append(f"Datacores: {', '.join(d['name'] for d in datacores)}")
    lines.append(f"Required Per Run: {datacores[0]['quantity'] if datacores else 0}")

    # Skills
    skills = sorted(inv_data['skills'], key=lambda s: s['name'].lower())
    lines.append(f"Skills: {', '.join(s['name'] for s in skills)}")

    lines.append(f"Time: {inv_data['time_minutes']}")

    # Probabilities table
    if tech_level == 'Tech II':
        base_bp_id = inv_data['invention_blueprint_id']
        base_runs = data.products.get(base_bp_id, {}).get(INVENTION, {}).get(type_id, 10)
        lines.extend([
            "Probabilities:",
            "| Decryptor                       | Probability | Runs | ME | TE |",
            "|---------------------------------|-------------|------|----|----|",
        ])
        for decryptor_name in sorted(DECRYPTORS.keys(), key=str.lower):
            prob = calculate_probability(inv_data['base_probability'], skill_level, decryptor_name)
            _, d_runs, d_me, d_te = DECRYPTORS[decryptor_name]
            lines.append(f"| {decryptor_name:<31} | {prob:>9.2f} | {base_runs + d_runs:>4} | {2 + d_me:>2} | {4 + d_te:>2} |")
    else:
        lines.extend([
            "Probabilities:",
            "| Invented From                | Decryptor                       | Probability | Runs | ME | TE |",
            "|------------------------------|---------------------------------|-------------|------|----|----|",
        ])
        for relic_name in ('Intact', 'Malfunctioning', 'Wrecked'):
            for decryptor_name in sorted(DECRYPTORS.keys(), key=str.lower):
                prob = calculate_probability(TECHIII_RELICS[relic_name], skill_level, decryptor_name)
                _, d_runs, d_me, d_te = DECRYPTORS[decryptor_name]
                lines.append(f"| {relic_name:<28} | {decryptor_name:<31} | {prob:>9.2f} | {TECHIII_BASE_RUNS[relic_name] + d_runs:>4} | {2 + d_me:>2} | {4 + d_te:>2} |")

    return '\n'.join(lines) + '\n'


def generate_report(data: SDEData, lookup_name: str,
                    material_efficiency: int = 0,
                    time_efficiency: int = 0,
                    display_waste: bool = False) -> str:
    """Generate recipe report for a product or blueprint."""
    # Resolve type: by name or blueprint name -> product
    type_data, type_id, found_via_bp = find_product_by_name(data, lookup_name)
    if not type_data:
        return f"Error: Item '{lookup_name}' not found or not published.\n"

    blueprint_id = type_id if found_via_bp else None

    # If blueprint, find display product
    display_type_id = type_id
    display_type_data = type_data
    if blueprint_id:
        for bp_id, activities in data.products.items():
            if bp_id != blueprint_id:
                continue
            for activity_dict in activities.values():
                if activity_dict:
                    product_id = next(iter(activity_dict.keys()))
                    product_data = data.types.get(product_id)
                    if product_data:
                        display_type_id = product_id
                        display_type_data = product_data
                        break
            if display_type_id != type_id:
                break

    recipe = get_recipe(data, display_type_id, material_efficiency)
    if not recipe:
        return f"Error: No industry activity found for '{lookup_name}'.\n"

    cat_name, group_name = get_group_path(data, display_type_id)
    mg_path = get_market_group_path(data, display_type_id)
    tech_level = get_tech_level(data, display_type_id)
    volume = get_volume(data, display_type_id)

    # Apply TE
    run_time = round(recipe['run_time'] * (1 - time_efficiency / 100)) if time_efficiency > 0 else recipe['run_time']

    def mat_row(mat):
        mat_name = get_type_name(data, mat['typeID']) or 'Unknown'
        buildable = 'Yes' if is_buildable(data, mat['typeID']) else 'No'
        if display_waste:
            waste = mat['original_quantity'] - mat['quantity']
            return f"| {mat_name} | {mat['quantity']} | {waste} | {buildable} |"
        return f"| {mat_name} | {mat['quantity']} | {buildable} |"

    mat_header = "| Item | Quantity | Waste | Buildable |" if display_waste else "| Item | Quantity | Buildable |"
    mat_sep = "|:-:|:-:|:-:|---:|" if display_waste else "|:-:|:---:|---:|"

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
        f"Run Time: {run_time}",
        mat_header,
        mat_sep,
    ]
    for mat in sorted(recipe['materials'], key=lambda m: (get_type_name(data, m['typeID']) or '').lower()):
        lines.append(mat_row(mat))

    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description='EVE Online Industrial Planner - Generate recipe reports from SDE')
    subparsers = parser.add_subparsers(dest='command', help='Command to run')

    recipe_parser = subparsers.add_parser('recipe', help='Generate recipe report')
    recipe_parser.add_argument('target', help='Product or Blueprint name (exact match, case-sensitive)')
    recipe_parser.add_argument('--sde', required=True, help='Path to SDE directory')
    recipe_parser.add_argument('--material-efficiency', '-me', type=int, default=0, help='Material Efficiency (0-10, default: 0)')
    recipe_parser.add_argument('--time-efficiency', '-te', type=int, default=0, help='Time Efficiency (even number 0-20, default: 0)')
    recipe_parser.add_argument('--display-waste', '-waste', action='store_true', help='Display waste due to ME not being max')

    invention_parser = subparsers.add_parser('invention', help='Generate invention report')
    invention_parser.add_argument('target', help='Tech II/Tech III product or blueprint name (exact match, case-sensitive)')
    invention_parser.add_argument('--sde', required=True, help='Path to SDE directory')
    invention_parser.add_argument('--skill', type=int, choices=range(1, 6), default=5, help='Skill level for all relevant skills (1-5, default: 5)')

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    sde_path = Path(args.sde)
    if not sde_path.exists():
        print(f"Error: SDE directory '{args.sde}' does not exist.", file=sys.stderr)
        sys.exit(1)

    data = parse_sde(sde_path)

    if args.command == 'recipe':
        if not 0 <= args.material_efficiency <= 10:
            print(f"Error: ME must be in range [0, 10], got {args.material_efficiency}", file=sys.stderr)
            sys.exit(1)
        if not 0 <= args.time_efficiency <= 20:
            print(f"Error: TE must be in range [0, 20], got {args.time_efficiency}", file=sys.stderr)
            sys.exit(1)
        if args.time_efficiency % 2 != 0:
            print(f"Error: TE must be an even integer, got {args.time_efficiency}", file=sys.stderr)
            sys.exit(1)
        print(generate_report(data, args.target, material_efficiency=args.material_efficiency,
                              time_efficiency=args.time_efficiency, display_waste=args.display_waste), end='')
    else:  # invention
        print(generate_invention_report(data, args.target, args.skill), end='')


if __name__ == '__main__':
    main()
