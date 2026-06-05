#!/usr/bin/env python3
import argparse
import bz2
import csv
import math
import os
import sys
import yaml
from collections import defaultdict
from typing import Dict, List, Optional, Tuple


class SDELoader:

    def __init__(self, sde_path: str):
        self.sde_path = sde_path
        self.types: Dict[int, Dict] = {}
        self.groups: Dict[int, Dict] = {}
        self.categories: Dict[int, Dict] = {}
        self.market_groups: Dict[int, Dict] = {}
        self.meta_types: Dict[int, int] = {}
        self.meta_groups: Dict[int, str] = {}
        self.activities: Dict[int, str] = {}
        self.ship_volumes: Dict[str, float] = {}

        self.blueprint_products: Dict[int, List[Dict]] = defaultdict(list)
        self.blueprint_materials: Dict[int, List[Dict]] = defaultdict(list)
        self.blueprint_times: Dict[int, Dict[int, int]] = defaultdict(dict)
        self.blueprint_skills: Dict[int, List[Dict]] = defaultdict(list)
        self.blueprint_probabilities: Dict[int, Dict] = defaultdict(dict)

        self.buildable_items: set = set()
        self.reaction_items: set = set()

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
        filepath = os.path.join(self.sde_path, 'ship_volumes.yaml')
        with open(filepath, 'r') as f:
            data = yaml.safe_load(f)
            if data:
                self.ship_volumes = {k: float(v) for k, v in data.items()}

    def _compute_buildable_items(self):
        for bp_type_id, products in self.blueprint_products.items():
            for product in products:
                if product['activityID'] == 1:
                    self.buildable_items.add(product['productTypeID'])
                elif product['activityID'] == 11:
                    self.reaction_items.add(product['productTypeID'])
                    self.buildable_items.add(product['productTypeID'])

    def find_type_by_name(self, name: str) -> Optional[Dict]:
        for type_info in self.types.values():
            if type_info['typeName'] == name and type_info['published']:
                return type_info
        return None

    def find_blueprint_for_product(self, product_type_id: int) -> Optional[int]:
        for bp_type_id, products in self.blueprint_products.items():
            for product in products:
                if product['productTypeID'] == product_type_id and product['activityID'] in (1, 11):
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
        if meta_group_name in ("Tech II", "Tech III"):
            return meta_group_name
        return "Tech I"

    def get_volume(self, type_info: Dict) -> float:
        group_info = self.groups.get(type_info['groupID'])

        if group_info:
            category_info = self.categories.get(group_info['categoryID'])

            if category_info and category_info['categoryName'] == 'Ship':
                packaged_volume = self.ship_volumes.get(group_info['groupName'])
                if packaged_volume is not None:
                    return packaged_volume

        return type_info['volume']

    def is_buildable(self, type_id: int) -> bool:
        return type_id in self.buildable_items

    def is_reaction(self, type_id: int) -> bool:
        return type_id in self.reaction_items

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

RELIC_BASELINES = {
    'Intact': {'base_chance': 0.26, 'base_runs': 20},
    'Malfunctioning': {'base_chance': 0.21, 'base_runs': 10},
    'Wrecked': {'base_chance': 0.14, 'base_runs': 3},
}

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


def parse_stockpile_file(filepath: str) -> Dict[str, int]:
    stocks = {}
    if not filepath or not os.path.exists(filepath):
        return stocks
    with open(filepath, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) >= 2:
                quantity_idx = -1
                for i, part in enumerate(parts[1:], 1):
                    try:
                        int(part)
                        quantity_idx = i
                        break
                    except ValueError:
                        continue

                if quantity_idx == -1:
                    continue

                name = ' '.join(parts[:quantity_idx])
                try:
                    quantity = int(parts[quantity_idx])
                    stocks[name] = quantity
                except ValueError:
                    continue
    return stocks


def load_config(filepath: str) -> Dict:
    if not filepath or not os.path.exists(filepath):
        return {}
    with open(filepath, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f) or {}


def calculate_invention_attempts(needed_runs: int, bp_output_runs: int, base_prob: float, skill_level: int) -> int:
    prob = base_prob * (1 + skill_level / 30 + skill_level / 40)
    prob = min(max(prob, 0), 1)

    expected_runs_per_attempt = prob * bp_output_runs

    if expected_runs_per_attempt <= 0:
        return needed_runs

    attempts = math.ceil(needed_runs / expected_runs_per_attempt)
    return attempts


class BuildPlanner:
    def __init__(self, sde: SDELoader, config: Dict):
        self.sde = sde
        self.config = config
        self.me = config.get('component_bpo_defaults', {}).get('me', 10)
        self.te = config.get('component_bpo_defaults', {}).get('te', 20)
        self.te_bonus = config.get('te_bonus', 20)
        self.invention_skill = config.get('invention_skill', 3)
        self.split_days = config.get('split_days', 30)

        # Results
        self.jobs = []  # List of {name, runs, days, count}
        self.materials_to_buy = defaultdict(int)
        self.stockpile_start = {}
        self.stockpile_end = {}
        self.stockpile_consumed = defaultdict(int)  # Track what was consumed from stock
        self.stockpile_produced = defaultdict(int)  # Track what was produced

    def plan(self, targets: Dict[str, int], stocks: Dict[str, int]):
        self.stockpile_start = dict(stocks)
        working_stocks = dict(stocks)

        for target_name, target_quantity in targets.items():
            self._process_target(target_name, target_quantity, working_stocks)

        self._aggregate_jobs()

        self._build_materials_list(working_stocks)

    def _process_target(self, target_name: str, target_quantity: int, stocks: Dict[str, int]):
        target_type = self.sde.find_type_by_name(target_name)
        if target_type is None:
            self.materials_to_buy[target_name] += target_quantity
            self.stockpile_end[target_name] = target_quantity
            return

        target_type_id = target_type['typeID']

        available = stocks.get(target_name, 0)

        if available >= target_quantity:
            # Already have enough, just consume from stock
            stocks[target_name] = available - target_quantity
            self.stockpile_end[target_name] = target_quantity
            return

        needed = target_quantity - available
        if available > 0:
            stocks[target_name] = 0

        # Build what we need
        self._build_item(target_name, needed, stocks)

        self.stockpile_end[target_name] = target_quantity

    def _build_item(self, item_name: str, quantity: int, stocks: Dict[str, int]):
        item_type = self.sde.find_type_by_name(item_name)
        if item_type is None:
            self.materials_to_buy[item_name] += quantity
            return

        item_type_id = item_type['typeID']

        if not self.sde.is_buildable(item_type_id):
            self.materials_to_buy[item_name] += quantity
            return

        blueprint_type_id = self.sde.find_blueprint_for_product(item_type_id)
        if blueprint_type_id is None:
            self.materials_to_buy[item_name] += quantity
            return

        blueprint_type = self.sde.types.get(blueprint_type_id)
        blueprint_name = blueprint_type['typeName'] if blueprint_type else item_name + " Blueprint"

        tech_level = self.sde.get_tech_level(item_type_id)

        if tech_level == 'Tech II':
            self._build_t2_item(item_name, quantity, stocks, blueprint_type_id, blueprint_name)
        else:
            self._build_t1_item(item_name, quantity, stocks, blueprint_type_id, blueprint_name)

    def _build_t1_item(self, item_name: str, quantity: int, stocks: Dict[str, int],
                       blueprint_type_id: int, blueprint_name: str):
        activity_id = 1
        products = self.sde.blueprint_products.get(blueprint_type_id, [])
        for product in products:
            if product['productTypeID'] == self.sde.find_type_by_name(item_name)['typeID']:
                activity_id = product['activityID']
                break

        materials, output_per_run, _ = self.sde.get_recipe(blueprint_type_id, activity_id)

        if output_per_run <= 0:
            self.materials_to_buy[item_name] += quantity
            return

        runs = math.ceil(quantity / output_per_run)

        time_seconds = self.sde.blueprint_times.get(blueprint_type_id, {}).get(activity_id, 0)
        if activity_id == 11:
            adjusted_time_seconds = time_seconds * (1 - self.te / 100)
        else:
            adjusted_time_seconds = time_seconds * (1 - self.te / 100) * (1 - self.te_bonus / 100)
        job_days = adjusted_time_seconds / 86400

        self.jobs.append({
            'name': blueprint_name,
            'runs': runs,
            'days': round(job_days * runs, 2),
            'count': runs * output_per_run
        })

        # Process materials
        for mat in materials:
            mat_name = mat['typeName']
            mat_needed = mat['quantity'] * runs

            if activity_id == 1:
                mat_needed = math.ceil(max(1, mat['quantity'] * (1 - self.me / 100))) * runs

            available_in_stock = stocks.get(mat_name, 0)
            if available_in_stock >= mat_needed:
                stocks[mat_name] = available_in_stock - mat_needed
                self.stockpile_consumed[mat_name] += mat_needed
                continue
            elif available_in_stock > 0:
                stocks[mat_name] = 0
                self.stockpile_consumed[mat_name] += available_in_stock
                mat_needed -= available_in_stock

            self._build_item(mat_name, mat_needed, stocks)

    def _build_t2_item(self, item_name: str, quantity: int, stocks: Dict[str, int],
                       blueprint_type_id: int, blueprint_name: str):
        item_type = self.sde.find_type_by_name(item_name)
        item_type_id = item_type['typeID']

        materials, output_per_run, _ = self.sde.get_recipe(blueprint_type_id, 1)

        if output_per_run <= 0:
            self.materials_to_buy[item_name] += quantity
            return

        runs = math.ceil(quantity / output_per_run)

        invention_bp_id, base_bp_id, bp_output_runs = find_invention_blueprint(self.sde, item_type_id)

        if invention_bp_id is None:
            self.materials_to_buy[item_name] += quantity
            return

        base_prob = self.sde.blueprint_probabilities.get(invention_bp_id, {}).get((8, blueprint_type_id), 0.34)
        invention_attempts = calculate_invention_attempts(runs, bp_output_runs, base_prob, self.invention_skill)

        invention_time_seconds = self.sde.blueprint_times.get(invention_bp_id, {}).get(8, 0)
        invention_days = invention_time_seconds / 86400

        base_bp_type = self.sde.types.get(invention_bp_id)
        base_bp_name = base_bp_type['typeName'] if base_bp_type else "Unknown Blueprint"

        self.jobs.append({
            'name': base_bp_name,
            'runs': invention_attempts,
            'days': round(invention_days * invention_attempts, 2),
            'count': invention_attempts * bp_output_runs
        })

        inv_materials, _ = get_invention_materials(self.sde, invention_bp_id, 8)
        for mat in inv_materials:
            self.materials_to_buy[mat['typeName']] += mat['quantity'] * invention_attempts

        time_seconds = self.sde.blueprint_times.get(blueprint_type_id, {}).get(1, 0)
        adjusted_time_seconds = time_seconds * (1 - self.te / 100) * (1 - self.te_bonus / 100)
        job_days = adjusted_time_seconds / 86400

        self.jobs.append({
            'name': blueprint_name,
            'runs': runs,
            'days': round(job_days * runs, 2),
            'count': runs * output_per_run
        })

        for mat in materials:
            mat_name = mat['typeName']
            mat_needed = math.ceil(max(1, mat['quantity'] * (1 - self.me / 100))) * runs

            available_in_stock = stocks.get(mat_name, 0)
            if available_in_stock >= mat_needed:
                stocks[mat_name] = available_in_stock - mat_needed
                self.stockpile_consumed[mat_name] += mat_needed
                continue
            elif available_in_stock > 0:
                stocks[mat_name] = 0
                self.stockpile_consumed[mat_name] += available_in_stock
                mat_needed -= available_in_stock

            self._build_item(mat_name, mat_needed, stocks)

    def _aggregate_jobs(self):
        job_aggregates = defaultdict(lambda: {'runs': 0, 'days': 0.0, 'count': 0})
        for job in self.jobs:
            name = job['name']
            job_aggregates[name]['runs'] += job['runs']
            job_aggregates[name]['days'] += job['days']
            job_aggregates[name]['count'] += job['count']

        self.aggregated_jobs = job_aggregates

    def _build_materials_list(self, stocks: Dict[str, int]):
        all_material_names = set(self.materials_to_buy.keys()) | set(self.stockpile_start.keys()) | set(self.stockpile_consumed.keys())

        items_produced = defaultdict(int)
        for job in self.jobs:
            bp_name = job['name']
            if 'Blueprint' in bp_name:
                item_name = bp_name.replace(' Blueprint', '')
            elif 'Reaction Formula' in bp_name:
                item_name = bp_name.replace(' Reaction Formula', '')
            else:
                continue
            items_produced[item_name] += job['count']

        self.materials_list = []
        for mat_name in sorted(all_material_names):
            type_info = self.sde.find_type_by_name(mat_name)

            start_amount = self.stockpile_start.get(mat_name, 0)

            consumed = self.stockpile_consumed.get(mat_name, 0)
            produced = items_produced.get(mat_name, 0)

            # If this is a target item, end_amount should be the target quantity
            if mat_name in self.stockpile_end:
                end_amount = self.stockpile_end[mat_name]
            else:
                end_amount = start_amount - consumed + produced

            to_buy = self.materials_to_buy.get(mat_name, 0)

            volume = 0.0
            if type_info:
                volume = self.sde.get_volume(type_info)

            buy_volume = to_buy * volume

            self.materials_list.append({
                'name': mat_name,
                'to_buy': to_buy,
                'buy_volume': buy_volume,
                'start_amount': start_amount,
                'end_amount': end_amount
            })


def handle_plan_command(sde: SDELoader, args):
    config = load_config(args.config) if args.config else {}

    # Parse stocks and targets
    stocks = parse_stockpile_file(args.stocks) if args.stocks else {}
    targets = parse_stockpile_file(args.targets) if args.targets else {}

    if not targets:
        print("Error: No targets specified", file=sys.stderr)
        sys.exit(1)

    # Create planner and run
    planner = BuildPlanner(sde, config)
    planner.plan(targets, stocks)

    # Write jobs.csv
    with open('jobs.csv', 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['name', 'runs', 'days', 'count'])
        for name in sorted(planner.aggregated_jobs.keys()):
            agg = planner.aggregated_jobs[name]
            writer.writerow([name, agg['runs'], round(agg['days'], 2), agg['count']])

    # Write materials.csv
    with open('materials.csv', 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['name', 'to_buy', 'buy_volume', 'start_amount', 'end_amount'])
        for mat in planner.materials_list:
            writer.writerow([
                mat['name'],
                mat['to_buy'],
                round(mat['buy_volume'], 1),
                mat['start_amount'],
                mat['end_amount']
            ])

    print(f"Generated jobs.csv and materials.csv")


def main():
    parser = argparse.ArgumentParser(description='EVE Online Industry Recipe Planner')
    parser.add_argument('command', choices=['recipe', 'invention', 'plan'], help='Command to execute')
    parser.add_argument('name', nargs='?', help='Product or Blueprint name (exact, case-sensitive)')
    parser.add_argument('--sde', required=True, help='Path to SDE directory')
    parser.add_argument('--skill', type=int, default=5, choices=[1, 2, 3, 4, 5],
                        help='Skill level for all relevant skills (1-5, default 5)')
    parser.add_argument('--material-efficiency', '-me', type=int, default=0, choices=range(0, 11),
                        help='Material Efficiency (0-10, default 0)')
    parser.add_argument('--time-efficiency', '-te', type=int, default=0,
                        help='Time Efficiency (even integer 0-20, default 0)')
    parser.add_argument('--display-waste', '-waste', action='store_true',
                        help='Display waste due to ME not being max')
    parser.add_argument('--stocks', help='Path to stockpile file')
    parser.add_argument('--targets', help='Path to targets file')
    parser.add_argument('--config', help='Path to config YAML file')

    args = parser.parse_args()

    if args.command != 'plan':
        if args.name is None:
            print("Error: 'name' argument is required for recipe and invention commands", file=sys.stderr)
            sys.exit(1)

    if args.command in ['recipe', 'invention']:
        if args.time_efficiency % 2 != 0:
            parser.error("--time-efficiency/-te must be an even integer")
        if args.time_efficiency < 0 or args.time_efficiency > 20:
            parser.error("--time-efficiency/-te must be in range [0, 20]")

    sde = SDELoader(args.sde)

    if args.command == 'recipe':
        handle_recipe_command(sde, args)
    elif args.command == 'invention':
        handle_invention_command(sde, args)
    elif args.command == 'plan':
        handle_plan_command(sde, args)


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

    me = args.material_efficiency
    te = args.time_efficiency
    display_waste = args.display_waste

    if activity_id == 1:
        for mat in materials:
            base_quantity = mat['quantity']
            mat['base_quantity'] = base_quantity
            adjusted_quantity = math.ceil(max(1, base_quantity * (1 - me / 100)))
            mat['quantity'] = adjusted_quantity
    else:
        for mat in materials:
            mat['base_quantity'] = mat['quantity']

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
            if product['activityID'] == 1:
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
    else:
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
        if product['activityID'] == 8:
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
