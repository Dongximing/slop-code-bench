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

    INVENTION = 8
    MANUFACTURING = 1

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

    def get_type_name(self, type_id: int) -> str:
        return self.types.get(type_id, {}).get('typeName', '')

    def get_group_path(self, type_id: int) -> tuple[str, str]:
        """Returns (category_name, group_name) or ('Unknown', 'Unknown')."""
        type_data = self.types.get(type_id)
        if not type_data:
            return 'Unknown', 'Unknown'
        group_data = self.groups.get(type_data['groupID'])
        if not group_data:
            return 'Unknown', 'Unknown'
        cat_data = self.categories.get(group_data['categoryID'])
        if cat_data:
            return cat_data['categoryName'], group_data['groupName']
        return 'Unknown', group_data['groupName']

    def get_market_group_path(self, type_id: int) -> Optional[str]:
        """Returns market group path as ' > '.joined string or None."""
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
        if meta_name == 'Tech II':
            return 'Tech II'
        if meta_name == 'Tech III':
            return 'Tech III'
        return 'Tech I'

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

    def get_product_for_invention(self, blueprint_id: int) -> Optional[int]:
        """Get the Tech II/III product typeID produced by invention from a blueprint."""
        prods = self.products.get(blueprint_id, {})
        if not prods or self.INVENTION not in prods:
            return None
        products = prods[self.INVENTION]
        if not products:
            return None
        return next(iter(products.keys()))

    def get_invention_data(self, product_id: int, skill_level: int) -> Optional[Dict]:
        """Get invention data for a Tech II or Tech III product."""
        # Find the blueprint that invents this product
        invention_blueprint_id = None
        for bp_id, prods in self.products.items():
            if self.INVENTION not in prods:
                continue
            if product_id in prods[self.INVENTION]:
                invention_blueprint_id = bp_id
                break

        if not invention_blueprint_id:
            return None

        # Get invention time
        time_minutes = math.ceil(self.activities.get(invention_blueprint_id, {}).get(self.INVENTION, 0) / 60)

        # Get materials
        datacore_materials = self.materials.get(invention_blueprint_id, {}).get(self.INVENTION, [])
        datacores = []
        for mat in datacore_materials:
            mat_name = self.get_type_name(mat['materialTypeID'])
            if mat_name and mat_name.startswith('Datacore - '):
                datacores.append({
                    'typeID': mat['materialTypeID'],
                    'name': mat_name,
                    'quantity': mat['quantity']
                })

        # Get skills
        skill_list = self.skills.get(invention_blueprint_id, {}).get(self.INVENTION, [])
        skills = []
        for skill in skill_list:
            skills.append({
                'name': self.get_type_name(skill['skillID']),
                'required_level': skill['level']
            })

        # Get base probability
        base_prob = 0.0
        prob_dict = self.probabilities.get(invention_blueprint_id, {}).get(self.INVENTION, {})
        if product_id in prob_dict:
            base_prob = prob_dict[product_id]

        return {
            'invention_blueprint_id': invention_blueprint_id,
            'time_minutes': time_minutes,
            'datacores': datacores,
            'skills': skills,
            'base_probability': base_prob,
            'tech_level': self.get_tech_level(product_id)
        }

    def get_recipe(self, type_id: int, material_efficiency: int = 0) -> Optional[Dict]:
        """Get manufacturing recipe for a type. Returns None if not manufacturable."""
        activities = self.products.get(type_id, {})
        if self.MANUFACTURING not in activities:
            return None
        time_raw = self.activities.get(type_id, {}).get(self.MANUFACTURING, 0)
        materials = self.materials.get(type_id, {}).get(self.MANUFACTURING, [])

        # Apply ME reduction to materials (only for Manufacturing)
        if material_efficiency > 0:
            adjusted_materials = []
            for mat in materials:
                # Calculate adjusted quantity: ceil(max(1, quantity * (1 - ME/100)))
                base_qty = mat['quantity']
                adjusted_qty = math.ceil(max(1, base_qty * (1 - material_efficiency / 100)))
                adjusted_materials.append({
                    'typeID': mat['typeID'],
                    'quantity': adjusted_qty,
                    'original_quantity': base_qty  # Store original for waste calculation
                })
            materials = adjusted_materials
        else:
            # Still store original for waste tracking
            materials = [{'typeID': mat['typeID'], 'quantity': mat['quantity'],
                          'original_quantity': mat['quantity']} for mat in materials]

        return {
            'activity': self.activity_names.get(self.MANUFACTURING, 'Manufacturing'),
            'output_qty': sum(activities[self.MANUFACTURING].values()),
            'run_time': round(time_raw / 60),
            'materials': materials
        }

    def is_buildable(self, type_id: int) -> bool:
        return type_id in self.buildable_types


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
    # Formula: 1 + skill/30 + skill/40 = 1 + 7*skill/120
    base_with_skills = base_prob * (1 + 7 * skill_level / 120)
    decryptor = DECRYPTORS.get(decryptor_name, (0, 0, 0, 0))
    prob_mult = decryptor[0] / 100.0
    return max(0.0, min(1.0, base_with_skills * (1 + prob_mult)))


def _match_name_prefix(names: List[str], prefix: str) -> List[str]:
    """Return names starting with prefix, case-insensitively."""
    prefix_lower = prefix.lower()
    return [n for n in names if n.lower().startswith(prefix_lower)]


def generate_invention_report(sde: SDEParser, lookup_name: str, skill_level: int) -> str:
    """Generate invention report for a Tech II or Tech III product."""

    type_data = sde.get_type_by_name(lookup_name)
    if not type_data:
        return f"Error: Item '{lookup_name}' not found or not published.\n"

    type_id = type_data['typeID']
    inv_data = sde.get_invention_data(type_id, skill_level)

    # Fallback: if direct lookup failed, try blueprint name
    if not inv_data and 'Blueprint' in type_data['typeName']:
        for bp_id, prods in sde.products.items():
            if sde.INVENTION not in prods:
                continue
            for prod_id in prods[sde.INVENTION].keys():
                candidate = sde.get_invention_data(prod_id, skill_level)
                if candidate:
                    inv_data = candidate
                    type_id = prod_id
                    type_data = sde.types.get(type_id)
                    break
            if inv_data:
                break

    if not inv_data:
        return f"Error: '{lookup_name}' is not an inventable item.\n"

    tech_level = inv_data['tech_level']
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

    if tech_level == 'Tech II':
        base_bp_id = inv_data['invention_blueprint_id']
        lines.append(f"Base: {sde.get_type_name(base_bp_id)} ({base_bp_id})")

    # Datacores
    datacores = sorted(inv_data['datacores'], key=lambda d: d['name'].lower())
    lines.append(f"Datacores: {', '.join(d['name'] for d in datacores)}")
    lines.append(f"Required Per Run: {datacores[0]['quantity'] if datacores else 0}")

    # Skills
    skills = sorted(inv_data['skills'], key=lambda s: s['name'].lower())
    lines.append(f"Skills: {', '.join(s['name'] for s in skills)}")

    # Time
    lines.append(f"Time: {inv_data['time_minutes']}")

    # Probabilities table
    if tech_level == 'Tech II':
        lines.extend([
            "Probabilities:",
            "| Decryptor                       | Probability | Runs | ME | TE |",
            "|---------------------------------|-------------|------|----|----|",
        ])
        base_bp_id = inv_data['invention_blueprint_id']
        base_runs = sde.products.get(base_bp_id, {}).get(sde.INVENTION, {}).get(type_id, 10)

        for decryptor_name in sorted(DECRYPTORS.keys(), key=str.lower):
            prob = calculate_probability(inv_data['base_probability'], skill_level, decryptor_name)
            _, d_runs, d_me, d_te = DECRYPTORS[decryptor_name]
            runs = base_runs + d_runs
            me = 2 + d_me
            te = 4 + d_te
            lines.append(f"| {decryptor_name:<31} | {prob:>9.2f} | {runs:>4} | {me:>2} | {te:>2} |")
    else:
        lines.extend([
            "Probabilities:",
            "| Invented From                | Decryptor                       | Probability | Runs | ME | TE |",
            "|------------------------------|---------------------------------|-------------|------|----|----|",
        ])

        for relic_name in ('Intact', 'Malfunctioning', 'Wrecked'):
            relic_prob = TECHIII_RELICS[relic_name]
            base_runs = TECHIII_BASE_RUNS[relic_name]

            for decryptor_name in sorted(DECRYPTORS.keys(), key=str.lower):
                prob = calculate_probability(relic_prob, skill_level, decryptor_name)
                _, d_runs, d_me, d_te = DECRYPTORS[decryptor_name]
                runs = base_runs + d_runs
                me = 2 + d_me
                te = 4 + d_te
                lines.append(f"| {relic_name:<28} | {decryptor_name:<31} | {prob:>9.2f} | {runs:>4} | {me:>2} | {te:>2} |")

    return '\n'.join(lines) + '\n'


def generate_report(sde: SDEParser, lookup_name: str,
                    material_efficiency: int = 0,
                    time_efficiency: int = 0,
                    display_waste: bool = False) -> str:
    """Generate recipe report for a product or blueprint."""

    # Primary lookup: by name (product or blueprint)
    type_data = sde.get_type_by_name(lookup_name)
    if not type_data:
        return f"Error: Item '{lookup_name}' not found or not published.\n"

    # Determine display type: if blueprint, show product; otherwise self
    # Also track the actual blueprint ID for materials lookup
    type_id = type_data['typeID']
    display_type_id = type_id
    display_type_data = type_data
    blueprint_id = type_id if 'Blueprint' in type_data['typeName'] else None

    if blueprint_id:
        for bp_id, prods in sde.products.items():
            if bp_id != type_id:
                continue
            for activity_dict in prods.values():
                if activity_dict:
                    product_id = next(iter(activity_dict.keys()))
                    product_data = sde.types.get(product_id)
                    if product_data:
                        display_type_id = product_id
                        display_type_data = product_data
                        break
            if display_type_id != type_id:
                break

    recipe = sde.get_recipe(display_type_id, material_efficiency=material_efficiency, blueprint_id=blueprint_id)
    if not recipe:
        return f"Error: No industry activity found for '{lookup_name}'.\n"

    cat_name, group_name = sde.get_group_path(display_type_id)
    mg_path = sde.get_market_group_path(display_type_id)
    tech_level = sde.get_tech_level(display_type_id)
    volume = sde.get_volume(display_type_id)

    # Apply TE to run time: run_time * (1 - TE/100)
    run_time = recipe['run_time']
    if time_efficiency > 0:
        run_time = round(run_time * (1 - time_efficiency / 100))

    if display_waste:
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
            "| Item | Quantity | Waste | Buildable |",
            "|:-:|:-:|:-:|---:|",
        ]

        for mat in sorted(recipe['materials'],
                         key=lambda m: (sde.get_type_name(m['typeID']) or '').lower()):
            mat_name = sde.get_type_name(mat['typeID']) or 'Unknown'
            buildable = 'Yes' if sde.is_buildable(mat['typeID']) else 'No'
            waste = mat['original_quantity'] - mat['quantity']
            lines.append(f"| {mat_name} | {mat['quantity']} | {waste} | {buildable} |")
    else:
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
            "| Item | Quantity | Buildable |",
            "|:-:|:---:|---:|",
        ]

        for mat in sorted(recipe['materials'],
                         key=lambda m: (sde.get_type_name(m['typeID']) or '').lower()):
            mat_name = sde.get_type_name(mat['typeID']) or 'Unknown'
            buildable = 'Yes' if sde.is_buildable(mat['typeID']) else 'No'
            lines.append(f"| {mat_name} | {mat['quantity']} | {buildable} |")

    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(
        description='EVE Online Industrial Planner - Generate recipe reports from SDE'
    )

    subparsers = parser.add_subparsers(dest='command', help='Command to run')

    recipe_parser = subparsers.add_parser('recipe', help='Generate recipe report')
    recipe_parser.add_argument('target', help='Product or Blueprint name (exact match, case-sensitive)')
    recipe_parser.add_argument('--sde', required=True, help='Path to SDE directory')
    recipe_parser.add_argument('--material-efficiency', '-me', type=int, default=0,
                               help='Material Efficiency (0-10, default: 0)')
    recipe_parser.add_argument('--time-efficiency', '-te', type=int, default=0,
                               help='Time Efficiency (even number 0-20, default: 0)')
    recipe_parser.add_argument('--display-waste', '-waste', action='store_true',
                               help='Display waste due to ME not being max')

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
        # Validate ME range
        if not 0 <= args.material_efficiency <= 10:
            print(f"Error: ME must be in range [0, 10], got {args.material_efficiency}", file=sys.stderr)
            sys.exit(1)
        # Validate TE range and evenness
        if not 0 <= args.time_efficiency <= 20:
            print(f"Error: TE must be in range [0, 20], got {args.time_efficiency}", file=sys.stderr)
            sys.exit(1)
        if args.time_efficiency % 2 != 0:
            print(f"Error: TE must be an even integer, got {args.time_efficiency}", file=sys.stderr)
            sys.exit(1)
        print(generate_report(sde, args.target,
                              material_efficiency=args.material_efficiency,
                              time_efficiency=args.time_efficiency,
                              display_waste=args.display_waste), end='')
    elif args.command == 'invention':
        print(generate_invention_report(sde, args.target, args.skill), end='')


if __name__ == '__main__':
    main()
