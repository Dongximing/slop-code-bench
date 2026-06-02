#!/usr/bin/env python3
"""EVE Online Industrial Planner - Parse SDE and emit recipe reports."""

import argparse
import bz2
import csv
import math
import sys
from pathlib import Path
from typing import Dict, List, Optional

import yaml


def _load_csv_bz2(filepath: Path):
    with bz2.open(filepath, 'rt', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def _load_yaml(filepath: Path):
    with open(filepath, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


class SDEParser:
    """Parses SDE files and builds lookup structures."""

    def __init__(self, sde_dir: Path):
        self.sde_dir = sde_dir
        self.types: Dict[int, Dict] = {}
        self.groups: Dict[int, Dict] = {}
        self.categories: Dict[int, Dict] = {}
        self.market_groups: Dict[int, Dict] = {}
        self.meta_types: Dict[int, int] = {}
        self.meta_groups: Dict[int, str] = {}
        self.activities: Dict[int, Dict[int, int]] = {}
        self.products: Dict[int, Dict[int, Dict[int, int]]] = {}
        self.materials: Dict[int, Dict[int, List[Dict]]] = {}
        self.probabilities: Dict[int, Dict[int, Dict[int, float]]] = {}
        self.skills: Dict[int, Dict[int, List[Dict]]] = {}
        self.activity_names: Dict[int, str] = {}
        self.ship_volumes: Dict[str, float] = {}
        self.buildable_types: set[int] = set()

        self.INVENTION = 8
        self.MANUFACTURING = 1

        self.load_all()

    def load_all(self):
        self._load_types()
        self._load_groups()
        self._load_categories()
        self._load_market_groups()
        self._load_meta_types()
        self._load_meta_groups()
        self._load_activities()
        self._load_products()
        self._load_materials()
        self._load_probabilities()
        self._load_skills()
        self._load_activity_names()
        self._load_ship_volumes()
        self._compute_buildable_types()

    def _load_types(self):
        for row in _load_csv_bz2(self.sde_dir / 'invTypes.csv.bz2'):
            if int(row.get('published', 0)) != 1:
                continue
            type_id = int(row['typeID'])
            self.types[type_id] = {
                'typeID': type_id,
                'groupID': int(row['groupID']),
                'typeName': row['typeName'],
                'volume': row.get('volume', '0'),
                'marketGroupID': int(row['marketGroupID']) if row.get('marketGroupID') and row['marketGroupID'] != 'None' else None,
                'mass': row.get('mass', '0'),
                'raceID': row.get('raceID'),
                'basePrice': row.get('basePrice'),
                'description': row.get('description', ''),
            }

    def _load_groups(self):
        for row in _load_csv_bz2(self.sde_dir / 'invGroups.csv.bz2'):
            if int(row.get('published', 0)) != 1:
                continue
            group_id = int(row['groupID'])
            self.groups[group_id] = {
                'groupID': group_id,
                'categoryID': int(row['categoryID']),
                'groupName': row['groupName'],
                'iconID': row.get('iconID'),
            }

    def _load_categories(self):
        for row in _load_csv_bz2(self.sde_dir / 'invCategories.csv.bz2'):
            if int(row.get('published', 0)) != 1:
                continue
            cat_id = int(row['categoryID'])
            self.categories[cat_id] = {
                'categoryID': cat_id,
                'categoryName': row['categoryName'],
                'iconID': row.get('iconID'),
            }

    def _load_market_groups(self):
        for row in _load_csv_bz2(self.sde_dir / 'invMarketGroups.csv.bz2'):
            mg_id = int(row['marketGroupID'])
            parent_id = int(row['parentGroupID']) if row.get('parentGroupID') and row['parentGroupID'] != 'None' else None
            self.market_groups[mg_id] = {
                'marketGroupID': mg_id,
                'parentGroupID': parent_id,
                'marketGroupName': row['marketGroupName'],
                'description': row.get('description', ''),
                'iconID': row.get('iconID'),
                'hasTypes': int(row.get('hasTypes', 0)) == 1,
            }

    def _load_meta_types(self):
        for row in _load_csv_bz2(self.sde_dir / 'invMetaTypes.csv.bz2'):
            if row.get('metaGroupID'):
                self.meta_types[int(row['typeID'])] = int(row['metaGroupID'])

    def _load_meta_groups(self):
        for row in _load_csv_bz2(self.sde_dir / 'invMetaGroups.csv.bz2'):
            self.meta_groups[int(row['metaGroupID'])] = row['metaGroupName']

    def _load_activities(self):
        for row in _load_csv_bz2(self.sde_dir / 'industryActivity.csv.bz2'):
            type_id = int(row['typeID'])
            activity_id = int(row['activityID'])
            self.activities.setdefault(type_id, {})[activity_id] = int(row['time'])

    def _load_products(self):
        for row in _load_csv_bz2(self.sde_dir / 'industryActivityProducts.csv.bz2'):
            type_id = int(row['typeID'])
            activity_id = int(row['activityID'])
            product_type_id = int(row['productTypeID'])
            quantity = int(row['quantity'])
            self.products.setdefault(type_id, {}).setdefault(activity_id, {})[product_type_id] = quantity

    def _load_materials(self):
        for row in _load_csv_bz2(self.sde_dir / 'industryActivityMaterials.csv.bz2'):
            type_id = int(row['typeID'])
            activity_id = int(row['activityID'])
            material = {'typeID': int(row['materialTypeID']), 'quantity': int(row['quantity'])}
            self.materials.setdefault(type_id, {}).setdefault(activity_id, []).append(material)

    def _load_probabilities(self):
        for row in _load_csv_bz2(self.sde_dir / 'industryActivityProbabilities.csv.bz2'):
            type_id = int(row['typeID'])
            activity_id = int(row['activityID'])
            product_type_id = int(row['productTypeID'])
            probability = float(row['probability'])
            self.probabilities.setdefault(type_id, {}).setdefault(activity_id, {})[product_type_id] = probability

    def _load_skills(self):
        for row in _load_csv_bz2(self.sde_dir / 'industryActivitySkills.csv.bz2'):
            type_id = int(row['typeID'])
            activity_id = int(row['activityID'])
            skill = {'skillID': int(row['skillID']), 'level': int(row['level'])}
            self.skills.setdefault(type_id, {}).setdefault(activity_id, []).append(skill)

    def _load_activity_names(self):
        for row in _load_csv_bz2(self.sde_dir / 'ramActivities.csv.bz2'):
            if row.get('published') == '1':
                self.activity_names[int(row['activityID'])] = row['activityName']

    def _load_ship_volumes(self):
        self.ship_volumes = {k: float(v) for k, v in _load_yaml(self.sde_dir / 'ship_volumes.yaml').items()}

    def _compute_buildable_types(self):
        for type_id, activities in self.products.items():
            if 1 in activities or 8 in activities:
                self.buildable_types.add(type_id)

    def get_type_by_name(self, name: str) -> Optional[Dict]:
        for type_data in self.types.values():
            if type_data['typeName'] == name:
                return type_data
        return None

    def get_type_name(self, type_id: int) -> Optional[str]:
        return self.types.get(type_id, {}).get('typeName')

    def get_group_path(self, type_id: int) -> tuple[str, str]:
        type_data = self.types.get(type_id)
        if not type_data:
            return ('Unknown', 'Unknown')
        group_data = self.groups.get(type_data['groupID'])
        if not group_data:
            return ('Unknown', 'Unknown')
        cat_data = self.categories.get(group_data['categoryID'])
        if not cat_data:
            return ('Unknown', group_data['groupName'])
        return (cat_data['categoryName'], group_data['groupName'])

    def get_market_group_path(self, type_id: int) -> Optional[str]:
        type_data = self.types.get(type_id)
        if not type_data or not type_data.get('marketGroupID'):
            return None
        path_parts = []
        mg_id = type_data['marketGroupID']
        while mg_id is not None:
            mg_data = self.market_groups.get(mg_id)
            if not mg_data:
                break
            path_parts.append(mg_data['marketGroupName'])
            mg_id = mg_data.get('parentGroupID')
        return ' > '.join(reversed(path_parts)) if path_parts else None

    def get_tech_level(self, type_id: int) -> str:
        meta_group_id = self.meta_types.get(type_id)
        if not meta_group_id:
            return 'Tech I'
        meta_name = self.meta_groups.get(meta_group_id, '')
        return 'Tech II' if meta_name == 'Tech II' else 'Tech III' if meta_name == 'Tech III' else 'Tech I'

    def get_volume(self, type_id: int) -> float:
        type_data = self.types.get(type_id)
        if not type_data:
            return 0.0
        _, group_name = self.get_group_path(type_id)
        if group_name in self.ship_volumes:
            return self.ship_volumes[group_name]
        try:
            return float(type_data.get('volume') or '0')
        except ValueError:
            return 0.0

    def get_product_for_invention(self, invention_blueprint_id: int) -> Optional[int]:
        """Get the Tech II/III product typeID produced by invention."""
        if self.INVENTION not in self.products.get(invention_blueprint_id, {}):
            return None
        products = self.products[invention_blueprint_id][self.INVENTION]
        if not products:
            return None
        # Return first (and typically only) product
        return next(iter(products.keys()))

    def get_invention_data(self, product_id: int, skill_level: int) -> Optional[Dict]:
        """
        Get invention data for a Tech II or Tech III product.
        For Tech II: Look up the invention blueprint that produces this product.
        For Tech III: Product is directly the relic, invention produces the Tech III item.
        """
        # First, check if this product is a Tech III item
        tech_level = self.get_tech_level(product_id)

        # Find the blueprint that invents this product (for Tech II)
        invention_blueprint_id = None
        for bp_id, prods in self.products.items():
            if self.INVENTION not in prods:
                continue
            if product_id in prods[self.INVENTION]:
                invention_blueprint_id = bp_id
                break

        if not invention_blueprint_id:
            # Not an invention blueprint, check if it's a Tech III that invents from relics
            # Tech III relics (Intact, Malfunctioning, Wrecked) are not meta group 7
            # Tech III items (like Strategic Cruisers) are usually meta group 7 or higher
            # They are NOT directly inventable, they're made from relics
            # So we need to check if there's an invention activity on relic items

            # Check if this is a Tech III item type (we'll detect via relic naming pattern)
            product_name = self.get_type_name(product_id) or ""

            # For Tech III, the relic is the input, not the output
            # So we look for inventions where product is a relic that produces the target
            for bp_id, prods in self.products.items():
                if self.INVENTION not in prods:
                    continue
                for prod_id, qty in prods[self.INVENTION].items():
                    if prod_id == product_id:
                        invention_blueprint_id = bp_id
                        break
                if invention_blueprint_id:
                    break

            if not invention_blueprint_id:
                return None

        # Get invention time
        time_minutes = math.ceil(self.activities.get(invention_blueprint_id, {}).get(self.INVENTION, 0) / 60)

        # Get materials and skills for the invention activity
        datacore_materials = self.materials.get(invention_blueprint_id, {}).get(self.INVENTION, [])

        # Group datacores by their base category (same name prefix)
        # E.g., "Datacore - Mechanical Engineering" and "Datacore - Nuclear Physics"
        datacores = []
        for mat in datacore_materials:
            mat_name = self.get_type_name(mat['materialTypeID']) or ""
            if mat_name.startswith("Datacore - "):
                datacores.append({
                    'typeID': mat['materialTypeID'],
                    'name': mat_name,
                    'quantity': mat['quantity']
                })

        # Get skills
        skill_list = self.skills.get(invention_blueprint_id, {}).get(self.INVENTION, [])
        skills = []
        for skill in skill_list:
            skill_name = self.get_type_name(skill['skillID']) or ""
            skills.append({
                'name': skill_name,
                'required_level': skill['level']
            })

        # Get base probability from industryActivityProbabilities
        base_prob = 0.0
        if invention_blueprint_id in self.probabilities and self.INVENTION in self.probabilities[invention_blueprint_id]:
            prob_dict = self.probabilities[invention_blueprint_id][self.INVENTION]
            # For Tech II, the probability is for producing the blueprint (product_id)
            # For Tech III, it varies by relic quality
            if product_id in prob_dict:
                base_prob = prob_dict[product_id]

        return {
            'invention_blueprint_id': invention_blueprint_id,
            'time_minutes': time_minutes,
            'datacores': datacores,
            'skills': skills,
            'base_probability': base_prob,
            'tech_level': tech_level
        }


# Decryptor modifiers: (probability_multiplier, runs, me, te)
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
    'Intact': 0.26,  # base probability
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
    # Skill modifier: (sum of required science + encryption) / 30 + encryption / 40
    # For simplicity, we use skill_level for both, but in reality they're different skills
    # The formula uses both science and encryption skills
    skill_mod = (skill_level / 30) + (skill_level / 40)

    # Base skill factor is already included in the formula
    # Standard skill: gives 1 + skill/30 + skill/40 = 1 + 2*skill/30 for level 5 = 1.333...
    # But actually, we need to calculate properly
    # For a skill level of N: 1 + N/30 + N/40 = 1 + (4N + 3N)/120 = 1 + 7N/120
    # Level 5: 1 + 35/120 = 1.2917

    base_with_skills = base_prob * (1 + 7 * skill_level / 120)

    # Apply decryptor
    decryptor = DECRYPTORS.get(decryptor_name, (0, 0, 0, 0))
    prob_mult = decryptor[0] / 100.0  # Convert percentage to multiplier

    final = base_with_skills * (1 + prob_mult)

    # Clamp to [0, 1]
    return max(0.0, min(1.0, final))


def generate_invention_report(sde: SDEParser, lookup_name: str, skill_level: int) -> str:
    """Generate invention report for a Tech II or Tech III product."""

    # Try direct lookup first
    type_data = sde.get_type_by_name(lookup_name)

    if not type_data:
        return f"Error: Item '{lookup_name}' not found or not published.\n"

    type_id = type_data['typeID']

    # Get invention data
    inv_data = sde.get_invention_data(type_id, skill_level)

    if not inv_data:
        # Check if it's a blueprint lookup for invention
        if 'Blueprint' in type_data['typeName']:
            # Try to find what product this blueprint creates via invention
            for bp_id, prods in sde.products.items():
                if bp_id == type_id and sde.INVENTION in prods:
                    for prod_id in prods[sde.INVENTION].keys():
                        inv_data = sde.get_invention_data(prod_id, skill_level)
                        if inv_data:
                            type_id = prod_id
                            type_data = sde.types.get(type_id)
                            break
                    if inv_data:
                        break

        if not inv_data:
            return f"Error: '{lookup_name}' is not an inventable item.\n"

    tech_level = sde.get_tech_level(type_id)

    if tech_level not in ('Tech II', 'Tech III'):
        return f"Error: '{lookup_name}' is not Tech II or Tech III.\n"

    cat_name, group_name = sde.get_group_path(type_id)
    mg_path = sde.get_market_group_path(type_id)

    lines = [
        f"ITEM: {type_data['typeName']} ({type_id})",
        f"Group: {cat_name} > {group_name}",
        f"Market Group: {mg_path if mg_path else 'None'}",
        f"Tech Level: {tech_level}",
        "",
        "Invention:",
    ]

    # For Tech II, show base blueprint info
    if tech_level == 'Tech II':
        base_bp_id = inv_data['invention_blueprint_id']
        base_bp_name = sde.get_type_name(base_bp_id) or "Unknown"
        lines.append(f"Base: {base_bp_name} ({base_bp_id})")

    # Datacores (sorted alphabetically, case-insensitive)
    datacores = sorted(inv_data['datacores'], key=lambda d: d['name'].lower())
    datacore_names = [d['name'] for d in datacores]
    lines.append(f"Datacores: {', '.join(datacore_names)}")

    # Count total datacores per run (all datacores use same quantity per run)
    if datacores:
        # The quantity is the same for all datacores per run
        total_per_run = datacores[0]['quantity']
    else:
        total_per_run = 0
    lines.append(f"Required Per Run: {total_per_run}")

    # Skills (sorted alphabetically, case-insensitive)
    skills = sorted(inv_data['skills'], key=lambda s: s['name'].lower())
    skill_names = [s['name'] for s in skills]
    lines.append(f"Skills: {', '.join(skill_names)}")

    # Time
    lines.append(f"Time: {inv_data['time_minutes']}")

    # Probabilities table
    lines.append("Probabilities:")

    if tech_level == 'Tech II':
        lines.append("| Decryptor                       | Probability | Runs | ME | TE |")
        lines.append("|---------------------------------|-------------|------|----|----|")

        for decryptor_name in sorted(DECRYPTORS.keys(), key=lambda x: x.lower()):
            prob = calculate_probability(inv_data['base_probability'], skill_level, decryptor_name)
            decryptor = DECRYPTORS[decryptor_name]

            # Base runs from product
            base_runs = sde.products.get(inv_data['invention_blueprint_id'], {}).get(sde.INVENTION, {}).get(type_id, 10)

            runs = base_runs + decryptor[1]
            me = 2 + decryptor[2]  # Base ME=2
            te = 4 + decryptor[3]  # Base TE=4

            lines.append(f"| {decryptor_name:<31} | {prob:>9.2f} | {runs:>4} | {me:>2} | {te:>2} |")
    else:  # Tech III
        lines.append("| Invented From                | Decryptor                       | Probability | Runs | ME | TE |")
        lines.append("|------------------------------|---------------------------------|-------------|------|----|----|")

        base_prob = inv_data['base_probability']

        for relic_name in ['Intact', 'Malfunctioning', 'Wrecked']:
            relic_base_prob = TECHIII_RELICS[relic_name]
            base_runs = TECHIII_BASE_RUNS[relic_name]

            for decryptor_name in sorted(DECRYPTORS.keys(), key=lambda x: x.lower()):
                prob = calculate_probability(relic_base_prob, skill_level, decryptor_name)
                decryptor = DECRYPTORS[decryptor_name]

                runs = base_runs + decryptor[1]
                me = 2 + decryptor[2]
                te = 4 + decryptor[3]

                lines.append(f"| {relic_name:<28} | {decryptor_name:<31} | {prob:>9.2f} | {runs:>4} | {me:>2} | {te:>2} |")

    return '\n'.join(lines) + '\n'


def generate_report(sde: SDEParser, lookup_name: str) -> str:
    """Generate recipe report for a product or blueprint."""

    type_data = sde.get_type_by_name(lookup_name)

    if not type_data:
        bp_id = _find_blueprint_producing(sde, lookup_name)
        if bp_id:
            type_data = sde.types[bp_id]

    if not type_data:
        return f"Error: Item '{lookup_name}' not found or not published.\n"

    type_id = type_data['typeID']
    display_type_id = type_id
    display_type_data = type_data

    if 'Blueprint' in type_data['typeName']:
        product_id = _find_product_of_blueprint(sde, type_id)
        if product_id:
            product_data = sde.types.get(product_id)
            if product_data:
                display_type_id = product_id
                display_type_data = product_data

    recipe = None
    if display_type_id != type_id:
        recipe = sde.get_recipe(type_id)
    if not recipe:
        recipe = sde.get_recipe(display_type_id)

    if not recipe:
        return f"Error: No industry activity found for '{lookup_name}'.\n"

    cat_name, group_name = sde.get_group_path(display_type_id)
    mg_path = sde.get_market_group_path(display_type_id)
    tech_level = sde.get_tech_level(display_type_id)
    volume = sde.get_volume(display_type_id)

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
        f"Run Time: {recipe['run_time']}",
        "| Item | Quantity | Buildable |",
        "|:-:|:---:|---:|",
    ]

    sorted_materials = sorted(
        recipe['materials'],
        key=lambda m: (sde.get_type_name(m['typeID']) or '').lower()
    )
    for mat in sorted_materials:
        mat_name = sde.get_type_name(mat['typeID']) or 'Unknown'
        buildable = 'Yes' if sde.is_buildable(mat['typeID']) else 'No'
        lines.append(f"| {mat_name} | {mat['quantity']} | {buildable} |")

    return '\n'.join(lines) + '\n'


def _find_product_of_blueprint(sde: SDEParser, blueprint_type_id: int) -> Optional[int]:
    """Return the product typeID of a blueprint, or None."""
    for activity_id, prod_dict in sde.products.get(blueprint_type_id, {}).items():
        if prod_dict:
            return next(iter(prod_dict.keys()))
    return None


def _find_blueprint_producing(sde: SDEParser, product_name: str) -> Optional[int]:
    """Find a blueprint that produces product_name, return its typeID."""
    for bp in sde.types.values():
        if 'Blueprint' not in bp['typeName']:
            continue
        for prod_id in sde.products.get(bp['typeID'], {}).values():
            if sde.get_type_name(next(iter(prod_id), 0)) == product_name:
                return bp['typeID']
    return None


def main():
    parser = argparse.ArgumentParser(
        description='EVE Online Industrial Planner - Generate recipe reports from SDE'
    )

    subparsers = parser.add_subparsers(dest='command', help='Command to run')

    # Recipe command
    recipe_parser = subparsers.add_parser('recipe', help='Generate recipe report')
    recipe_parser.add_argument('target', help='Product or Blueprint name (exact match, case-sensitive)')
    recipe_parser.add_argument('--sde', required=True, help='Path to SDE directory')

    # Invention command
    invention_parser = subparsers.add_parser('invention', help='Generate invention report')
    invention_parser.add_argument('target', help='Tech II/Tech III product or blueprint name (exact match, case-sensitive)')
    invention_parser.add_argument('--sde', required=True, help='Path to SDE directory')
    invention_parser.add_argument('--skill', type=int, choices=range(1, 6), default=5,
                                   help='Skill level for all relevant skills (1-5, default: 5)')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    sde_path = Path(args.sde)
    if not sde_path.exists():
        print(f"Error: SDE directory '{args.sde}' does not exist.", file=sys.stderr)
        sys.exit(1)

    sde = SDEParser(sde_path)

    if args.command == 'recipe':
        print(generate_report(sde, args.target), end='')
    elif args.command == 'invention':
        print(generate_invention_report(sde, args.target, args.skill), end='')


if __name__ == '__main__':
    main()
