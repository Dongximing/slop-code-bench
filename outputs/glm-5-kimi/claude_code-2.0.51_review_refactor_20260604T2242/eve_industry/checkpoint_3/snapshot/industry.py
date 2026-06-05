#!/usr/bin/env python3
import argparse
import bz2
import csv
import math
import os
import sys
from collections import defaultdict
from typing import Dict, List, Optional, Tuple


class SDELoader:

    def __init__(self, sde_path: str):
        self.sde_path = sde_path
        self.types: Dict[int, Dict] = {}  # typeID -> type info
        self.groups: Dict[int, Dict] = {}  # groupID -> group info
        self.categories: Dict[int, Dict] = {}  # categoryID -> category info
        self.market_groups: Dict[int, Dict] = {}  # marketGroupID -> market group info
        self.meta_types: Dict[int, int] = {}  # typeID -> metaGroupID
        self.meta_groups: Dict[int, str] = {}  # metaGroupID -> metaGroupName
        self.activities: Dict[int, str] = {}  # activityID -> activityName
        self.ship_volumes: Dict[str, float] = {}  # groupName -> packaged volume

        # Industry data - keyed by blueprint typeID
        self.blueprint_products: Dict[int, List[Dict]] = defaultdict(list)  # bpTypeID -> list of products
        self.blueprint_materials: Dict[int, List[Dict]] = defaultdict(list)  # bpTypeID -> list of materials
        self.blueprint_times: Dict[int, Dict[int, int]] = defaultdict(dict)  # bpTypeID -> {activityID: time}
        self.blueprint_skills: Dict[int, List[Dict]] = defaultdict(list)  # bpTypeID -> list of skills
        self.blueprint_probabilities: Dict[int, Dict] = defaultdict(dict)  # bpTypeID -> {(activityID, productTypeID): probability}

        # Buildable items (items that can be produced via industry)
        self.buildable_items: set = set()

        self._load_all()

    def _load_all(self):
        self._load_types()
        self._load_groups()
        self._load_categories()
        self._load_market_groups()
        self._load_meta_types()
        self._load_meta_groups()
        self._load_activities()
        self._load_industry_products()
        self._load_industry_materials()
        self._load_industry_activities()
        self._load_industry_skills()
        self._load_industry_probabilities()
        self._load_ship_volumes()
        self._compute_buildable_items()

    def _read_csv_bz2(self, filename: str) -> List[Dict]:
        filepath = os.path.join(self.sde_path, filename)
        with bz2.open(filepath, 'rt', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            return list(reader)

    def _load_types(self):
        for row in self._read_csv_bz2('invTypes.csv.bz2'):
            type_id = int(row['typeID'])
            published_val = row['published'].lower() in ('true', '1')

            # Parse volume - handle various formats
            volume_str = row['volume']
            try:
                if volume_str and volume_str != 'None' and volume_str != '0E-10':
                    volume = float(volume_str)
                else:
                    volume = 0.0
            except ValueError:
                volume = 0.0

            self.types[type_id] = {
                'typeID': type_id,
                'groupID': int(row['groupID']),
                'typeName': row['typeName'],
                'description': row.get('description', ''),
                'mass': row.get('mass', '0'),
                'volume': volume,
                'capacity': row.get('capacity', '0'),
                'portionSize': int(row['portionSize']) if row['portionSize'] else 1,
                'raceID': row.get('raceID'),
                'basePrice': row.get('basePrice'),
                'published': published_val,
                'marketGroupID': int(row['marketGroupID']) if row['marketGroupID'] and row['marketGroupID'] != 'None' else None,
                'iconID': row.get('iconID'),
                'soundID': row.get('soundID'),
                'graphicID': row.get('graphicID'),
            }

    def _load_groups(self):
        for row in self._read_csv_bz2('invGroups.csv.bz2'):
            group_id = int(row['groupID'])
            self.groups[group_id] = {
                'groupID': group_id,
                'categoryID': int(row['categoryID']),
                'groupName': row['groupName'],
            }

    def _load_categories(self):
        for row in self._read_csv_bz2('invCategories.csv.bz2'):
            category_id = int(row['categoryID'])
            self.categories[category_id] = {
                'categoryID': category_id,
                'categoryName': row['categoryName'],
            }

    def _load_market_groups(self):
        for row in self._read_csv_bz2('invMarketGroups.csv.bz2'):
            group_id = int(row['marketGroupID'])
            parent_id = int(row['parentGroupID']) if row['parentGroupID'] and row['parentGroupID'] != 'None' else None
            self.market_groups[group_id] = {
                'marketGroupID': group_id,
                'parentGroupID': parent_id,
                'marketGroupName': row['marketGroupName'],
            }

    def _load_meta_types(self):
        for row in self._read_csv_bz2('invMetaTypes.csv.bz2'):
            type_id = int(row['typeID'])
            meta_group_id = int(row['metaGroupID']) if row['metaGroupID'] and row['metaGroupID'] != 'None' else None
            if meta_group_id:
                self.meta_types[type_id] = meta_group_id

    def _load_meta_groups(self):
        for row in self._read_csv_bz2('invMetaGroups.csv.bz2'):
            group_id = int(row['metaGroupID'])
            self.meta_groups[group_id] = row['metaGroupName']

    def _load_activities(self):
        for row in self._read_csv_bz2('ramActivities.csv.bz2'):
            activity_id = int(row['activityID'])
            self.activities[activity_id] = row['activityName']

    def _load_industry_products(self):
        for row in self._read_csv_bz2('industryActivityProducts.csv.bz2'):
            bp_type_id = int(row['typeID'])
            activity_id = int(row['activityID'])
            product_type_id = int(row['productTypeID'])
            quantity = int(row['quantity'])
            self.blueprint_products[bp_type_id].append({
                'activityID': activity_id,
                'productTypeID': product_type_id,
                'quantity': quantity,
            })

    def _load_industry_materials(self):
        for row in self._read_csv_bz2('industryActivityMaterials.csv.bz2'):
            bp_type_id = int(row['typeID'])
            activity_id = int(row['activityID'])
            material_type_id = int(row['materialTypeID'])
            quantity = int(row['quantity'])
            self.blueprint_materials[bp_type_id].append({
                'activityID': activity_id,
                'materialTypeID': material_type_id,
                'quantity': quantity,
            })

    def _load_industry_activities(self):
        for row in self._read_csv_bz2('industryActivity.csv.bz2'):
            bp_type_id = int(row['typeID'])
            activity_id = int(row['activityID'])
            time_val = int(row['time'])
            self.blueprint_times[bp_type_id][activity_id] = time_val

    def _load_industry_skills(self):
        for row in self._read_csv_bz2('industryActivitySkills.csv.bz2'):
            bp_type_id = int(row['typeID'])
            activity_id = int(row['activityID'])
            skill_id = int(row['skillID'])
            level = int(row['level'])
            self.blueprint_skills[bp_type_id].append({
                'activityID': activity_id,
                'skillID': skill_id,
                'level': level,
            })

    def _load_industry_probabilities(self):
        for row in self._read_csv_bz2('industryActivityProbabilities.csv.bz2'):
            bp_type_id = int(row['typeID'])
            activity_id = int(row['activityID'])
            product_type_id = int(row['productTypeID'])
            probability = float(row['probability'])
            self.blueprint_probabilities[bp_type_id][(activity_id, product_type_id)] = probability

    def _load_ship_volumes(self):
        import yaml

        filepath = os.path.join(self.sde_path, 'ship_volumes.yaml')
        with open(filepath, 'r') as f:
            data = yaml.safe_load(f)
            if data:
                self.ship_volumes = {k: float(v) for k, v in data.items()}

    def _compute_buildable_items(self):
        # An item is buildable if it appears as a product in industryActivityProducts
        for bp_type_id, products in self.blueprint_products.items():
            for product in products:
                # Manufacturing (activity 1) or Reactions (activity 11)
                if product['activityID'] in (1, 11):
                    self.buildable_items.add(product['productTypeID'])

    def find_type_by_name(self, name: str) -> Optional[Dict]:
        for type_info in self.types.values():
            if type_info['typeName'] == name and type_info['published']:
                return type_info
        return None

    def find_blueprint_for_product(self, product_type_id: int) -> Optional[int]:
        for bp_type_id, products in self.blueprint_products.items():
            for product in products:
                if product['productTypeID'] == product_type_id:
                    # Only return for Manufacturing or Reactions
                    if product['activityID'] in (1, 11):
                        return bp_type_id
        return None

    def is_blueprint(self, type_info: Dict) -> bool:
        group_info = self.groups.get(type_info['groupID'])
        return group_info is not None and group_info['categoryID'] == 9

    def get_market_group_path(self, market_group_id: Optional[int]) -> List[str]:
        if market_group_id is None:
            return []

        path = []
        current_id = market_group_id
        while current_id is not None:
            group_info = self.market_groups.get(current_id)
            if group_info is None:
                break
            path.append(group_info['marketGroupName'])
            current_id = group_info['parentGroupID']

        return list(reversed(path))

    def get_tech_level(self, type_id: int) -> str:
        meta_group_id = self.meta_types.get(type_id)
        if meta_group_id is None:
            return "Tech I"

        meta_group_name = self.meta_groups.get(meta_group_id, "")
        if meta_group_name == "Tech II":
            return "Tech II"
        if meta_group_name == "Tech III":
            return "Tech III"
        return "Tech I"

    def get_volume(self, type_info: Dict) -> float:
        group_info = self.groups.get(type_info['groupID'])

        if group_info:
            category_info = self.categories.get(group_info['categoryID'])

            # Check if this is a ship (category 6)
            if category_info and category_info['categoryName'] == 'Ship':
                packaged_volume = self.ship_volumes.get(group_info['groupName'])
                if packaged_volume is not None:
                    return packaged_volume

        return type_info['volume']

    def is_buildable(self, type_id: int) -> bool:
        return type_id in self.buildable_items

    def get_recipe(self, blueprint_type_id: int, activity_id: int) -> Tuple[List[Dict], int, int]:
        materials = []
        for mat in self.blueprint_materials.get(blueprint_type_id, []):
            if mat['activityID'] == activity_id:
                type_info = self.types.get(mat['materialTypeID'])
                if type_info:
                    materials.append({
                        'typeID': mat['materialTypeID'],
                        'typeName': type_info['typeName'],
                        'quantity': mat['quantity'],
                        'buildable': self.is_buildable(mat['materialTypeID']),
                    })

        output_quantity = 0
        for product in self.blueprint_products.get(blueprint_type_id, []):
            if product['activityID'] == activity_id:
                output_quantity = product['quantity']
                break

        time_seconds = self.blueprint_times.get(blueprint_type_id, {}).get(activity_id, 0)
        run_time_minutes = math.ceil(time_seconds / 60)

        return materials, output_quantity, run_time_minutes


def format_materials_table(materials: List[Dict], me: int = 0, display_waste: bool = False) -> str:
    sorted_materials = sorted(materials, key=lambda m: (m['typeName'].lower(), m['typeName']))

    if display_waste:
        lines = [
            "| Item | Quantity |Waste | Buildable |",
            "|:---:|:---:|:---:|---:|",
        ]
    else:
        lines = [
            "| Item | Quantity | Buildable |",
            "|:-:|:---:|---:|",
        ]

    for mat in sorted_materials:
        buildable_str = "Yes" if mat['buildable'] else "No"
        quantity = mat['quantity']
        base_quantity = mat.get('base_quantity', quantity)
        waste = base_quantity - quantity

        if display_waste:
            lines.append(f"| {mat['typeName']} | {quantity} |{waste}| {buildable_str} |")
        else:
            lines.append(f"| {mat['typeName']} | {quantity} | {buildable_str} |")

    return "\n".join(lines)


# Decryptor modifiers for invention/reverse engineering
DECRYPTORS = {
    'None': {'prob_mult': 0, 'runs': 0, 'me': 0, 'te': 0},
    'Accelerant Decryptor': {'prob_mult': 20, 'runs': 1, 'me': 2, 'te': 10},
    'Attainment Decryptor': {'prob_mult': 80, 'runs': 4, 'me': -1, 'te': 4},
    'Augmentation Decryptor': {'prob_mult': -40, 'runs': 9, 'me': -2, 'te': 2},
    'Optimized Attainment Decryptor': {'prob_mult': 90, 'runs': 2, 'me': 1, 'te': -2},
    'Optimized Augmentation Decryptor': {'prob_mult': -10, 'runs': 7, 'me': 2, 'te': 0},
    'Parity Decryptor': {'prob_mult': 50, 'runs': 3, 'me': 1, 'te': -2},
    'Process Decryptor': {'prob_mult': 10, 'runs': 0, 'me': 3, 'te': 6},
    'Symmetry Decryptor': {'prob_mult': 0, 'runs': 2, 'me': 1, 'te': 8},
}

# Decryptor list in sorted order for output
DECRYPTOR_ORDER = [
    'None',
    'Accelerant Decryptor',
    'Attainment Decryptor',
    'Augmentation Decryptor',
    'Optimized Attainment Decryptor',
    'Optimized Augmentation Decryptor',
    'Parity Decryptor',
    'Process Decryptor',
    'Symmetry Decryptor',
]

# Tech III relic baselines
RELIC_BASELINES = {
    'Intact': {'base_chance': 0.26, 'base_runs': 20},
    'Malfunctioning': {'base_chance': 0.21, 'base_runs': 10},
    'Wrecked': {'base_chance': 0.14, 'base_runs': 3},
}

# Relic order for sorting
RELIC_ORDER = ['Intact', 'Malfunctioning', 'Wrecked']


def get_invention_skills(sde: SDELoader, bp_type_id: int, activity_id: int) -> List[Dict]:
    skills = []
    for skill in sde.blueprint_skills.get(bp_type_id, []):
        if skill['activityID'] == activity_id:
            skill_info = sde.types.get(skill['skillID'])
            if skill_info:
                skills.append({
                    'skillID': skill['skillID'],
                    'skillName': skill_info['typeName'],
                })
    return skills


def get_invention_materials(sde: SDELoader, bp_type_id: int, activity_id: int) -> Tuple[List[Dict], int]:
    materials = []
    quantity_per_run = 0

    for mat in sde.blueprint_materials.get(bp_type_id, []):
        if mat['activityID'] == activity_id:
            mat_type_info = sde.types.get(mat['materialTypeID'])
            if mat_type_info:
                # Check if this is a datacore (groupID 333)
                group_info = sde.groups.get(mat_type_info['groupID'])
                if group_info and group_info['groupID'] == 333:
                    materials.append({
                        'typeID': mat['materialTypeID'],
                        'typeName': mat_type_info['typeName'],
                        'quantity': mat['quantity'],
                    })
                    quantity_per_run = mat['quantity']

    return materials, quantity_per_run


def get_invention_time(sde: SDELoader, bp_type_id: int, activity_id: int) -> int:
    time_seconds = sde.blueprint_times.get(bp_type_id, {}).get(activity_id, 0)
    return math.ceil(time_seconds / 60)


def calculate_invention_probability(
    sde: SDELoader,
    base_prob: float,
    skills: List[Dict],
    decryptor_prob_mult: float,
    skill_level: int
) -> float:
    num_science_skills = 0
    has_encryption = False

    for skill in skills:
        skill_info = sde.types.get(skill['skillID'])
        if skill_info:
            if 'Encryption' in skill_info['typeName']:
                has_encryption = True
            else:
                num_science_skills += 1

    science_term = (num_science_skills * skill_level) / 30
    encryption_term = skill_level / 40 if has_encryption else 0

    prob = base_prob * (1 + science_term + encryption_term) * (1 + decryptor_prob_mult / 100)

    return min(max(prob, 0), 1)


def format_probability(prob: float) -> str:
    return f"{prob * 100:.2f}"


def find_invention_blueprint(sde: SDELoader, product_type_id: int) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    t2_bp_id = None
    for bp_type_id, products in sde.blueprint_products.items():
        for product in products:
            if product['productTypeID'] == product_type_id and product['activityID'] == 1:
                t2_bp_id = bp_type_id
                break
        if t2_bp_id:
            break

    if not t2_bp_id:
        return None, None, None

    invention_bp_id = None
    output_runs = None

    for bp_type_id, products in sde.blueprint_products.items():
        for product in products:
            if product['productTypeID'] == t2_bp_id and product['activityID'] == 8:
                invention_bp_id = bp_type_id
                output_runs = product['quantity']
                break
        if invention_bp_id:
            break

    if not invention_bp_id:
        return None, None, None

    base_bp_id = invention_bp_id

    return invention_bp_id, base_bp_id, output_runs


def find_reverse_engineering_relics(sde: SDELoader, product_type_id: int) -> List[Tuple[int, str, str, int]]:
    relics = []

    t3_bp_id = None
    for bp_type_id, products in sde.blueprint_products.items():
        for product in products:
            if product['productTypeID'] == product_type_id and product['activityID'] == 1:
                t3_bp_id = bp_type_id
                break
        if t3_bp_id:
            break

    if not t3_bp_id:
        return []

    for bp_type_id, products in sde.blueprint_products.items():
        for product in products:
            if product['productTypeID'] == t3_bp_id and product['activityID'] == 8:
                relic_info = sde.types.get(bp_type_id)
                if relic_info:
                    relic_name = relic_info['typeName']
                    quality = None
                    for q in RELIC_ORDER:
                        if relic_name.startswith(q):
                            quality = q
                            break
                    if quality:
                        relics.append((bp_type_id, relic_name, quality, product['quantity']))
                break

    return relics


def main():
    parser = argparse.ArgumentParser(description='EVE Online Industry Recipe Planner')
    parser.add_argument('command', choices=['recipe', 'invention'], help='Command to execute')
    parser.add_argument('name', help='Product or Blueprint name (exact, case-sensitive)')
    parser.add_argument('--sde', required=True, help='Path to SDE directory')
    parser.add_argument('--skill', type=int, default=5, choices=[1, 2, 3, 4, 5],
                        help='Skill level for all relevant skills (1-5, default 5)')
    parser.add_argument('--material-efficiency', '-me', type=int, default=0, choices=range(0, 11),
                        help='Material Efficiency (0-10, default 0)')
    parser.add_argument('--time-efficiency', '-te', type=int, default=0,
                        help='Time Efficiency (even integer 0-20, default 0)')
    parser.add_argument('--display-waste', '-waste', action='store_true',
                        help='Display waste due to ME not being max')

    args = parser.parse_args()

    # Validate TE is even and in range
    if hasattr(args, 'time_efficiency'):
        if args.time_efficiency % 2 != 0:
            parser.error("--time-efficiency/-te must be an even integer")
        if args.time_efficiency < 0 or args.time_efficiency > 20:
            parser.error("--time-efficiency/-te must be in range [0, 20]")

    # Load SDE
    sde = SDELoader(args.sde)

    if args.command == 'recipe':
        handle_recipe_command(sde, args)
    elif args.command == 'invention':
        handle_invention_command(sde, args)


def handle_recipe_command(sde: SDELoader, args):
    type_info = sde.find_type_by_name(args.name)
    if type_info is None:
        print(f"Error: Item '{args.name}' not found", file=sys.stderr)
        sys.exit(1)

    type_id = type_info['typeID']

    if sde.is_blueprint(type_info):
        blueprint_type_id = type_id

        products = sde.blueprint_products.get(blueprint_type_id, [])
        product_info = None
        activity_id = None

        for activity in [1, 11]:
            for product in products:
                if product['activityID'] == activity:
                    product_type_id = product['productTypeID']
                    product_info = sde.types.get(product_type_id)
                    activity_id = activity
                    break
            if product_info:
                break

        if product_info is None:
            print(f"Error: No product found for blueprint '{args.name}'", file=sys.stderr)
            sys.exit(1)

        product_type_id = product_info['typeID']
    else:
        blueprint_type_id = sde.find_blueprint_for_product(type_id)
        if blueprint_type_id is None:
            print(f"Error: No blueprint found for product '{args.name}'", file=sys.stderr)
            sys.exit(1)

        blueprint_info = sde.types.get(blueprint_type_id)
        product_info = type_info

        products = sde.blueprint_products.get(blueprint_type_id, [])
        activity_id = None
        for product in products:
            if product['productTypeID'] == type_id:
                activity_id = product['activityID']
                break

        if activity_id is None:
            print(f"Error: Could not determine activity for '{args.name}'", file=sys.stderr)
            sys.exit(1)

    activity_name = sde.activities.get(activity_id, "Unknown")

    materials, output_quantity, run_time = sde.get_recipe(blueprint_type_id, activity_id)

    # Get ME and TE from args
    me = getattr(args, 'material_efficiency', 0)
    te = getattr(args, 'time_efficiency', 0)
    display_waste = getattr(args, 'display_waste', False)

    # Apply ME only to Manufacturing (activity 1)
    if activity_id == 1:  # Manufacturing
        for mat in materials:
            base_quantity = mat['quantity']
            mat['base_quantity'] = base_quantity
            # Formula: Amount needed = ceil(max(1, quantity required per run * (1 - ME/100) * num_runs))
            # Since num_runs is always 1: ceil(max(1, base_quantity * (1 - ME/100)))
            adjusted_quantity = math.ceil(max(1, base_quantity * (1 - me / 100)))
            mat['quantity'] = adjusted_quantity
    else:
        # For Reactions (activity 11), ME does not apply
        for mat in materials:
            mat['base_quantity'] = mat['quantity']

    # Apply TE to run time (time reduction at seconds level)
    # run_time is in minutes, convert to seconds for TE calculation
    time_seconds = sde.blueprint_times.get(blueprint_type_id, {}).get(activity_id, 0)
    adjusted_time_seconds = time_seconds * (1 - te / 100)
    run_time = math.ceil(adjusted_time_seconds / 60)

    product_type_id = product_info['typeID']
    product_name = product_info['typeName']

    group_info = sde.groups.get(product_info['groupID'])
    category_id = group_info['categoryID'] if group_info else None
    category_info = sde.categories.get(category_id) if category_id else None

    category_name = category_info['categoryName'] if category_info else "Unknown"
    group_name = group_info['groupName'] if group_info else "Unknown"

    market_group_id = product_info.get('marketGroupID')
    market_group_path = sde.get_market_group_path(market_group_id)
    market_group_str = " > ".join(market_group_path) if market_group_path else "None"

    tech_level = sde.get_tech_level(product_type_id)

    volume = sde.get_volume(product_info)

    if volume == int(volume):
        volume_str = f"{volume:.2f}"
    elif volume >= 1:
        volume_str = f"{volume:.2f}".rstrip('0').rstrip('.')
    else:
        volume_str = f"{volume:.3f}".rstrip('0').rstrip('.')

    print(f"ITEM: {product_name} ({product_type_id})")
    print(f"Group: {category_name} > {group_name}")
    print(f"Market Group: {market_group_str}")
    print(f"Tech Level: {tech_level}")
    print(f"Volume: {volume_str}")
    print()
    print("Recipe:")
    print(f"Activity: {activity_name}")
    print(f"Output Quantity: {output_quantity}")
    print(f"Run Time: {run_time}")
    print(format_materials_table(materials, me, display_waste))


def handle_invention_command(sde: SDELoader, args):
    type_info = sde.find_type_by_name(args.name)
    if type_info is None:
        print(f"Error: Item '{args.name}' not found", file=sys.stderr)
        sys.exit(1)

    type_id = type_info['typeID']
    skill_level = args.skill

    if sde.is_blueprint(type_info):
        products = sde.blueprint_products.get(type_id, [])
        product_type_id = None
        for product in products:
            if product['activityID'] == 1:  # Manufacturing
                product_type_id = product['productTypeID']
                break
        if product_type_id is None:
            print(f"Error: No manufacturing product found for blueprint '{args.name}'", file=sys.stderr)
            sys.exit(1)
        type_id = product_type_id
        type_info = sde.types.get(type_id)

    tech_level = sde.get_tech_level(type_id)

    if tech_level not in ['Tech II', 'Tech III']:
        print(f"Error: '{args.name}' is not a Tech II or Tech III item", file=sys.stderr)
        sys.exit(1)

    product_name = type_info['typeName']

    group_info = sde.groups.get(type_info['groupID'])
    category_id = group_info['categoryID'] if group_info else None
    category_info = sde.categories.get(category_id) if category_id else None

    category_name = category_info['categoryName'] if category_info else "Unknown"
    group_name = group_info['groupName'] if group_info else "Unknown"

    market_group_id = type_info.get('marketGroupID')
    market_group_path = sde.get_market_group_path(market_group_id)
    market_group_str = " > ".join(market_group_path) if market_group_path else "None"

    if tech_level == 'Tech II':
        handle_tech2_invention(sde, type_id, product_name, category_name, group_name,
                               market_group_str, skill_level)
    else:  # Tech III
        handle_tech3_reverse_engineering(sde, type_id, product_name, category_name, group_name,
                                         market_group_str, skill_level)


def handle_tech2_invention(sde: SDELoader, product_type_id: int, product_name: str,
                           category_name: str, group_name: str, market_group_str: str,
                           skill_level: int):
    invention_bp_id, base_bp_id, output_runs = find_invention_blueprint(sde, product_type_id)

    if invention_bp_id is None:
        print(f"Error: No invention blueprint found for '{product_name}'", file=sys.stderr)
        sys.exit(1)

    base_bp_info = sde.types.get(base_bp_id)
    base_bp_name = base_bp_info['typeName'] if base_bp_info else "Unknown"

    invention_products = sde.blueprint_products.get(invention_bp_id, [])
    t2_bp_product_id = None
    for product in invention_products:
        if product['activityID'] == 8:  # Invention
            t2_bp_product_id = product['productTypeID']
            break

    base_prob = sde.blueprint_probabilities.get(invention_bp_id, {}).get((8, t2_bp_product_id), 0)

    materials, quantity_per_run = get_invention_materials(sde, invention_bp_id, 8)

    skills = get_invention_skills(sde, invention_bp_id, 8)

    time_minutes = get_invention_time(sde, invention_bp_id, 8)

    sorted_datacores = sorted(materials, key=lambda m: (m['typeName'].lower(), m['typeName']))
    datacore_names = [m['typeName'] for m in sorted_datacores]

    sorted_skills = sorted(skills, key=lambda s: (s['skillName'].lower(), s['skillName']))
    skill_names = [s['skillName'] for s in sorted_skills]

    base_runs = output_runs

    prob_rows = []
    for decryptor_name in DECRYPTOR_ORDER:
        decryptor = DECRYPTORS[decryptor_name]
        prob = calculate_invention_probability(sde, base_prob, skills, decryptor['prob_mult'], skill_level)
        runs = base_runs + decryptor['runs']
        me = 2 + decryptor['me']
        te = 4 + decryptor['te']
        prob_rows.append({
            'decryptor': decryptor_name,
            'probability': prob,
            'runs': runs,
            'me': me,
            'te': te,
        })

    print(f"ITEM: {product_name} ({product_type_id})")
    print(f"Group: {category_name} > {group_name}")
    print(f"Market Group: {market_group_str}")
    print(f"Tech Level: Tech II")
    print()
    print("Invention:")
    print(f"Base: {base_bp_name} ({base_bp_id})")
    print(f"Datacores: {', '.join(datacore_names)}")
    print(f"Required Per Run: {quantity_per_run}")
    print(f"Skills: {', '.join(skill_names)}")
    print(f"Time: {time_minutes}")
    print("Probabilities:")
    print("| Decryptor                       | Probability | Runs | ME | TE |")
    print("|---------------------------------|-------------|------|----|----|")
    for row in prob_rows:
        decryptor_name = row['decryptor']
        prob_str = f"{row['probability']*100:.2f}"
        runs_str = str(row['runs'])
        me_str = str(row['me'])
        te_str = str(row['te'])
        print(f"| {decryptor_name:31}| {prob_str:11}| {runs_str:4}| {me_str:2}| {te_str:2}|")


def handle_tech3_reverse_engineering(sde: SDELoader, product_type_id: int, product_name: str,
                                     category_name: str, group_name: str, market_group_str: str,
                                     skill_level: int):
    relics = find_reverse_engineering_relics(sde, product_type_id)

    if not relics:
        print(f"Error: No relics found for '{product_name}'", file=sys.stderr)
        sys.exit(1)

    relics_sorted = sorted(relics, key=lambda r: RELIC_ORDER.index(r[2]))

    first_relic_id = relics_sorted[0][0]
    materials, quantity_per_run = get_invention_materials(sde, first_relic_id, 8)
    skills = get_invention_skills(sde, first_relic_id, 8)

    time_minutes = get_invention_time(sde, first_relic_id, 8)

    sorted_datacores = sorted(materials, key=lambda m: (m['typeName'].lower(), m['typeName']))
    datacore_names = [m['typeName'] for m in sorted_datacores]

    sorted_skills = sorted(skills, key=lambda s: (s['skillName'].lower(), s['skillName']))
    skill_names = [s['skillName'] for s in sorted_skills]

    prob_rows = []
    for relic_id, relic_name, relic_quality, base_runs in relics_sorted:
        baseline = RELIC_BASELINES[relic_quality]
        base_prob = baseline['base_chance']

        for decryptor_name in DECRYPTOR_ORDER:
            decryptor = DECRYPTORS[decryptor_name]
            prob = calculate_invention_probability(sde, base_prob, skills, decryptor['prob_mult'], skill_level)
            runs = base_runs + decryptor['runs']
            me = 2 + decryptor['me']
            te = 4 + decryptor['te']
            prob_rows.append({
                'relic': relic_name,
                'relic_quality': relic_quality,
                'decryptor': decryptor_name,
                'probability': prob,
                'runs': runs,
                'me': me,
                'te': te,
            })

    print(f"ITEM: {product_name} ({product_type_id})")
    print(f"Group: {category_name} > {group_name}")
    print(f"Market Group: {market_group_str}")
    print(f"Tech Level: Tech III")
    print()
    print("Invention:")
    print(f"Datacores: {', '.join(datacore_names)}")
    print(f"Required Per Run: {quantity_per_run}")
    print(f"Skills: {', '.join(skill_names)}")
    print(f"Time: {time_minutes}")
    print("Probabilities:")
    print("| Invented From                | Decryptor                       | Probability | Runs | ME | TE |")
    print("|------------------------------|---------------------------------|-------------|------|----|----|")
    for row in prob_rows:
        relic_name = row['relic']
        decryptor_name = row['decryptor']
        prob_str = f"{row['probability']*100:.2f}"
        runs_str = str(row['runs'])
        me_str = str(row['me'])
        te_str = str(row['te'])
        print(f"| {relic_name:28}| {decryptor_name:31}| {prob_str:11}| {runs_str:4}| {me_str:2}| {te_str:2}|")


if __name__ == '__main__':
    main()
