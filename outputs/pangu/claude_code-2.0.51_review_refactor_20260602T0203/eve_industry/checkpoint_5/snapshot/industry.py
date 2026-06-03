#!/usr/bin/env python3
"""
EVE Online Industry Recipe Generator with --build-all support
Parses Static Data Export (SDE) and emits recipe reports for products or blueprints.
"""

import argparse
import bz2
import csv
import math
from collections import defaultdict, deque
from decimal import Decimal
from pathlib import Path

import yaml


def load_yaml(path):
    with open(path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def load_csv_bz2(path):
    with bz2.open(path, 'rt', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        return list(reader)


# Invention activity ID
INVENTION_ACTIVITY_ID = 8
# Reverse Engineering activity ID
REVERSE_ENGINEERING_ACTIVITY_ID = 7

# Decryptor definitions
DECRYPTORS = [
    {"name": "Accelerant Decryptor", "prob_mult": 20, "runs": 1, "me": 2, "te": 10},
    {"name": "Attainment Decryptor", "prob_mult": 80, "runs": 4, "me": -1, "te": 4},
    {"name": "Augmentation Decryptor", "prob_mult": -40, "runs": 9, "me": -2, "te": 2},
    {"name": "Optimized Attainment Decryptor", "prob_mult": 90, "runs": 2, "me": 1, "te": -2},
    {"name": "Optimized Augmentation Decryptor", "prob_mult": -10, "runs": 7, "me": 2, "te": 0},
    {"name": "Parity Decryptor", "prob_mult": 50, "runs": 3, "me": 1, "te": -2},
    {"name": "Process Decryptor", "prob_mult": 10, "runs": 0, "me": 3, "te": 6},
    {"name": "Symmetry Decryptor", "prob_mult": 0, "runs": 2, "me": 1, "te": 8},
]
NONE_DECRYPTOR = {"name": "None", "prob_mult": 0, "runs": 0, "me": 0, "te": 0}

# Tech III relic baselines
TECH_III_RELIC_BASELINES = {
    "Intact": {"base_chance": 0.26, "base_runs": 20},
    "Malfunctioning": {"base_chance": 0.21, "base_runs": 10},
    "Wrecked": {"base_chance": 0.14, "base_runs": 3},
}

ENCRYPTION_SKILL_KEYWORDS = ["encryption"]

# Capital ship group IDs (for T1 capital ships)
CAPITAL_SHIP_GROUP_IDS = {883}  # Capital Industrial Ship
CAPITAL_COMPONENT_GROUP_IDS = {873, 913}  # Capital Construction Components, Advanced Capital Construction Components
CAPITAL_BLUEPRINT_GROUP_IDS = {915, 914, 944}  # Capital Construction Blueprints, Advanced Capital Construction Component Blueprints, Capital Industrial Ship Blueprint


def split_jobs_algorithm(N, max_runs, days_per_run, split_days):
    """
    Split N total runs into jobs that don't exceed max_runs.
    Algorithm from specification:
    - max_runs = floor(split_days / days_per_run)
    - t = ceil(N / max_runs)
    - B = N // t
    - x = N % t
    - if B + 1 == max_runs and x > t/2 and N // (t+1) + 1 < max_runs:
        t = t + 1
        B = N // t
        x = N % t
    - output: x jobs of (B+1), (t-x) jobs of B
    """
    if N <= max_runs:
        return [N]

    max_runs_calculated = math.floor(split_days / days_per_run)

    if N <= max_runs_calculated:
        return [N]

    t = math.ceil(N / max_runs_calculated)
    B = N // t
    x = N % t

    # Check the special adjustment condition
    if B + 1 == max_runs_calculated and x > t / 2 and (N // (t + 1)) + 1 < max_runs_calculated:
        t = t + 1
        B = N // t
        x = N % t

    result = []
    # x jobs of size (B+1)
    for _ in range(x):
        result.append(B + 1)
    # (t-x) jobs of size B
    for _ in range(t - x):
        if B > 0:
            result.append(B)

    return result


class SDE:
    def __init__(self, sde_dir):
        self.sde_dir = Path(sde_dir)
        self._load_all()

    def _load_all(self):
        types_path = self.sde_dir / "invTypes.csv.bz2"
        raw_types = load_csv_bz2(types_path)
        self.types_by_id = {}
        self.types_by_name = {}
        for t in raw_types:
            if t['published'] == '1':
                type_id = int(t['typeID'])
                name = t['typeName']
                self.types_by_id[type_id] = t
                self.types_by_name[name] = t

        groups_path = self.sde_dir / "invGroups.csv.bz2"
        raw_groups = load_csv_bz2(groups_path)
        self.groups_by_id = {}
        for g in raw_groups:
            if g['published'] == '1':
                self.groups_by_id[int(g['groupID'])] = g

        cats_path = self.sde_dir / "invCategories.csv.bz2"
        raw_cats = load_csv_bz2(cats_path)
        self.categories_by_id = {}
        for c in raw_cats:
            if c['published'] == '1':
                self.categories_by_id[int(c['categoryID'])] = c

        mg_path = self.sde_dir / "invMarketGroups.csv.bz2"
        raw_mgs = load_csv_bz2(mg_path)
        self.market_groups_by_id = {}
        self.market_groups_by_parent = defaultdict(list)
        for mg in raw_mgs:
            mg_id = int(mg['marketGroupID'])
            parent_id = mg['parentGroupID']
            if parent_id and parent_id != 'None':
                parent_id = int(parent_id)
                mg['parentGroupID'] = parent_id
                self.market_groups_by_parent[parent_id].append(mg_id)
            self.market_groups_by_id[mg_id] = mg

        metatypes_path = self.sde_dir / "invMetaTypes.csv.bz2"
        raw_metatypes = load_csv_bz2(metatypes_path)
        self.meta_types_by_type = {}
        for mt in raw_metatypes:
            type_id = int(mt['typeID'])
            self.meta_types_by_type[type_id] = mt

        activity_path = self.sde_dir / "industryActivity.csv.bz2"
        self.activities = {}
        for a in load_csv_bz2(activity_path):
            type_id = int(a['typeID'])
            activity_id = int(a['activityID'])
            self.activities[(type_id, activity_id)] = a

        products_path = self.sde_dir / "industryActivityProducts.csv.bz2"
        self.products = []
        for p in load_csv_bz2(products_path):
            self.products.append({
                'typeID': int(p['typeID']),
                'activityID': int(p['activityID']),
                'productTypeID': int(p['productTypeID']),
                'quantity': int(p['quantity'])
            })

        materials_path = self.sde_dir / "industryActivityMaterials.csv.bz2"
        self.materials = []
        for m in load_csv_bz2(materials_path):
            self.materials.append({
                'typeID': int(m['typeID']),
                'activityID': int(m['activityID']),
                'materialTypeID': int(m['materialTypeID']),
                'quantity': int(m['quantity'])
            })

        ship_vol_path = self.sde_dir / "ship_volumes.yaml"
        if ship_vol_path.exists():
            self.ship_volumes = load_yaml(ship_vol_path)
        else:
            self.ship_volumes = {}

        skills_path = self.sde_dir / "invTypes.csv.bz2"
        raw_types = load_csv_bz2(skills_path)
        self.skills_by_id = {}
        for t in raw_types:
            if t['groupID'] == '270':  # Skill group
                type_id = int(t['typeID'])
                self.skills_by_id[type_id] = t

    def get_activity_time(self, type_id, activity_id):
        entry = self.activities.get((type_id, activity_id))
        if entry:
            return int(entry['time'])
        return None

    def get_products(self, type_id, activity_id):
        return [p for p in self.products if p['typeID'] == type_id and p['activityID'] == activity_id]

    def get_materials(self, type_id, activity_id):
        return [m for m in self.materials if m['typeID'] == type_id and m['activityID'] == activity_id]

    def get_blueprint_for_product(self, product_type_id):
        for p in self.products:
            if p['productTypeID'] == product_type_id and p['activityID'] in (1, 11):
                return p['typeID']
        return None

    def is_buildable(self, type_id):
        for p in self.products:
            if p['productTypeID'] == type_id and p['activityID'] in (1, 11):
                return True
        return False

    def get_tech_level(self, type_id):
        meta = self.meta_types_by_type.get(type_id)
        if not meta:
            return "Tech I"
        meta_group_id = int(meta['metaGroupID'])
        if meta_group_id == 2:
            return "Tech II"
        elif meta_group_id == 14:
            return "Tech III"
        return "Tech I"

    def get_volume(self, type_id, group_id):
        t = self.types_by_id.get(type_id, {})
        vol_str = t.get('volume', '0')
        g = self.groups_by_id.get(group_id, {})
        group_name = g.get('groupName', '')
        if group_name in self.ship_volumes:
            return float(self.ship_volumes[group_name])
        try:
            return float(vol_str)
        except ValueError:
            return 0.0

    def get_group_hierarchy(self, type_id):
        t = self.types_by_id.get(type_id)
        if not t:
            return ("Unknown", "Unknown")
        g = self.groups_by_id.get(int(t['groupID']))
        if not g:
            return ("Unknown", "Unknown")
        cat = self.categories_by_id.get(int(g['categoryID']))
        if not cat:
            return ("Unknown", g.get('groupName', 'Unknown'))
        return (cat.get('categoryName', 'Unknown'), g.get('groupName', 'Unknown'))

    def is_ship_volume(self, type_id, group_id):
        g = self.groups_by_id.get(group_id, {})
        group_name = g.get('groupName', '')
        return group_name in self.ship_volumes

    def get_market_group_path(self, type_id):
        t = self.types_by_id.get(type_id)
        if not t:
            return None
        mg_id_str = t.get('marketGroupID')
        if not mg_id_str or mg_id_str == 'None':
            return None
        mg_id = int(mg_id_str)
        path = []
        current_id = mg_id
        while current_id is not None:
            mg = self.market_groups_by_id.get(current_id)
            if not mg:
                break
            path.append(mg['marketGroupName'])
            parent_id = mg.get('parentGroupID')
            current_id = parent_id
        path.reverse()
        return path

    def get_invention_data(self, target_type_id):
        probs_path = self.sde_dir / "industryActivityProbabilities.csv.bz2"
        base_prob = None
        blueprint_id = None
        blueprint_name = None
        raw_probs = load_csv_bz2(probs_path)
        for prob in raw_probs:
            if (int(prob['productTypeID']) == target_type_id and
                int(prob['activityID']) == INVENTION_ACTIVITY_ID):
                blueprint_id = int(prob['typeID'])
                base_prob = float(prob['probability'])
                break
        if blueprint_id is None:
            return None
        bp_t = self.types_by_id.get(blueprint_id, {})
        blueprint_name = bp_t.get('typeName', f"Unknown ({blueprint_id})")
        skills = self._get_invention_skills(blueprint_id)
        materials = self.get_materials(blueprint_id, INVENTION_ACTIVITY_ID)
        datacores = []
        for m in materials:
            mat_type_id = m['materialTypeID']
            mat_t = self.types_by_id.get(mat_type_id, {})
            mat_name = mat_t.get('typeName', f"Unknown ({mat_type_id})")
            datacores.append({
                'typeID': mat_type_id,
                'name': mat_name,
                'quantity': m['quantity']
            })
        datacores.sort(key=lambda x: x['name'].lower())
        time_sec = self.get_activity_time(blueprint_id, INVENTION_ACTIVITY_ID)
        if time_sec is None:
            return None
        run_time_min = math.ceil(time_sec / 60)
        return {
            'blueprint_id': blueprint_id,
            'blueprint_name': blueprint_name,
            'skills': skills,
            'datacores': datacores,
            'required_per_run': datacores[0]['quantity'] if datacores else 0,
            'time': run_time_min,
            'base_prob': base_prob,
            'tech_level': self.get_tech_level(target_type_id)
        }

    def _get_invention_skills(self, blueprint_id):
        skills_path = self.sde_dir / "industryActivitySkills.csv.bz2"
        raw_skills = load_csv_bz2(skills_path)
        skills = []
        for s in raw_skills:
            if (int(s['typeID']) == blueprint_id and
                int(s['activityID']) == INVENTION_ACTIVITY_ID):
                skill_id = int(s['skillID'])
                level = int(s['level'])
                skill_t = self.skills_by_id.get(skill_id)
                if skill_t:
                    skill_name = skill_t.get('typeName', f"Unknown ({skill_id})")
                    skills.append({
                        'typeID': skill_id,
                        'name': skill_name,
                        'level': level
                    })
        skills.sort(key=lambda x: x['name'].lower())
        return skills

    def get_tech3_relic_type(self, target_type_id):
        tech_level = self.get_tech_level(target_type_id)
        if tech_level != "Tech III":
            return None
        relic_products = []
        for p in self.products:
            if p['productTypeID'] == target_type_id and p['activityID'] == REVERSE_ENGINEERING_ACTIVITY_ID:
                relic_products.append(p)
        if not relic_products:
            return None
        relic_type_id = None
        for m in self.materials:
            if (m['typeID'] == target_type_id and
                m['activityID'] == REVERSE_ENGINEERING_ACTIVITY_ID):
                relic_type_id = m['materialTypeID']
                break
        if relic_type_id is None:
            return None
        relic_t = self.types_by_id.get(relic_type_id, {})
        relic_name = relic_t.get('typeName', f"Unknown ({relic_type_id})")
        if 'Intact' in relic_name:
            qual = "Intact"
        elif 'Malfunctioning' in relic_name:
            qual = "Malfunctioning"
        elif 'Wrecked' in relic_name:
            qual = "Wrecked"
        else:
            qual = "Wrecked"
        baseline = TECH_III_RELIC_BASELINES.get(qual, TECH_III_RELIC_BASELINES['Wrecked'])
        return (relic_name, baseline['base_chance'], baseline['base_runs'])

    def is_t1_capital(self, type_id):
        """Check if this is a T1 capital ship or blueprint."""
        t = self.types_by_id.get(type_id, {})
        group_id = int(t.get('groupID', 0))
        return group_id in CAPITAL_SHIP_GROUP_IDS or group_id in CAPITAL_BLUEPRINT_GROUP_IDS or group_id in CAPITAL_COMPONENT_GROUP_IDS

    def get_group_id(self, type_id):
        t = self.types_by_id.get(type_id, {})
        return int(t.get('groupID', 0))


def find_target(sde: SDE, name: str) -> int:
    if name in sde.types_by_name:
        return int(sde.types_by_name[name]['typeID'])
    raise ValueError(f"Unknown product or blueprint: {name}")


def get_recipe(sde: SDE, target_type_id: int, target_name: str, me: int = 0, te: int = 0) -> dict:
    """Extract recipe for a product or blueprint."""
    blueprint_id = sde.get_blueprint_for_product(target_type_id)
    if blueprint_id is not None:
        is_product = True
    else:
        blueprint_id = target_type_id
        is_product = False

    manufact_mat = sde.get_materials(blueprint_id, 1)
    reaction_mat = sde.get_materials(blueprint_id, 11)
    if manufact_mat:
        activity_id = 1
        activity_name = "Manufacturing"
    elif reaction_mat:
        activity_id = 11
        activity_name = "Reactions"
    else:
        raise ValueError(f"No industry activity found for: {target_name}")

    products = sde.get_products(blueprint_id, activity_id)
    if not products:
        raise ValueError(f"No products defined for blueprint: bp_id={blueprint_id}")

    if len(products) == 1:
        output_qty = products[0]['quantity']
        output_type_id = products[0]['productTypeID']
        output_name = sde.types_by_id[output_type_id]['typeName']
    else:
        if is_product:
            matching = [p for p in products if p['productTypeID'] == target_type_id]
            if matching:
                output_qty = matching[0]['quantity']
                output_type_id = matching[0]['productTypeID']
                output_name = target_name
            else:
                output_qty = products[0]['quantity']
                output_type_id = products[0]['productTypeID']
                output_name = sde.types_by_id[output_type_id]['typeName']
        else:
            output_qty = products[0]['quantity']
            output_type_id = products[0]['productTypeID']
            output_name = sde.types_by_id[output_type_id]['typeName']

    time_sec = sde.get_activity_time(blueprint_id, activity_id)
    if time_sec is None:
        raise ValueError(f"No activity time for blueprint {blueprint_id}")

    if activity_id == 1:
        run_time_sec = time_sec * (1 - te / 100)
    else:
        run_time_sec = time_sec
    run_time_min = math.ceil(run_time_sec / 60)

    materials = sde.get_materials(blueprint_id, activity_id)
    material_list = []
    for m in materials:
        mat_type_id = m['materialTypeID']
        mat_t = sde.types_by_id.get(mat_type_id, {})
        mat_name = mat_t.get('typeName', f"Unknown ({mat_type_id})")
        buildable = sde.is_buildable(mat_type_id)
        if activity_id == 1:
            base_qty = m['quantity']
            reduced_qty = base_qty * (1 - me / 100)
            adjusted_qty = math.ceil(max(1, reduced_qty))
        else:
            adjusted_qty = m['quantity']
        material_entry = {
            'typeID': mat_type_id,
            'name': mat_name,
            'quantity': adjusted_qty,
            'buildable': 'Yes' if buildable else 'No'
        }
        material_list.append(material_entry)

    group_id = int(sde.types_by_id[output_type_id]['groupID'])
    is_ship = sde.is_ship_volume(output_type_id, group_id)
    volume = sde.get_volume(output_type_id, group_id)

    return {
        'item_name': output_name,
        'item_type_id': output_type_id,
        'group': sde.get_group_hierarchy(output_type_id),
        'market_group_path': sde.get_market_group_path(output_type_id),
        'tech_level': sde.get_tech_level(output_type_id),
        'volume': volume,
        'is_ship': is_ship,
        'activity': activity_name,
        'output_quantity': output_qty,
        'run_time': run_time_min,
        'blueprint_id': blueprint_id,
        'materials': sorted(material_list, key=lambda x: x['name'].lower()),
    }


def format_recipe(recipe, display_waste=False):
    lines = []
    lines.append(f"ITEM: {recipe['item_name']} ({recipe['item_type_id']})")
    lines.append(f"Group: {recipe['group'][0]} > {recipe['group'][1]}")
    if recipe['market_group_path']:
        lines.append(f"Market Group: {' > '.join(recipe['market_group_path'])}")
    else:
        lines.append("Market Group: None")
    lines.append(f"Tech Level: {recipe['tech_level']}")
    vol = recipe['volume']
    if recipe.get('is_ship', False):
        lines.append(f"Volume: {vol:.2f}")
    else:
        d = Decimal(str(vol)).normalize()
        vol_str = format(d, 'f')
        if '.' in vol_str:
            vol_str = vol_str.rstrip('0').rstrip('.')
        lines.append(f"Volume: {vol_str}")
    lines.append("")
    lines.append("Recipe:")
    lines.append(f"Activity: {recipe['activity']}")
    lines.append(f"Output Quantity: {recipe['output_quantity']}")
    lines.append(f"Run Time: {recipe['run_time']}")
    if display_waste:
        lines.append("| Item      | Quantity  |Waste   | Buildable |")
        lines.append("|:---------:|:---------:|:-----:|----------:|")
        for m in recipe['materials']:
            waste = m.get('waste', 0)
            lines.append(f"| {m['name']} | {m['quantity']} | {waste}  | {m['buildable']} |")
    else:
        lines.append("| Item | Quantity | Buildable |")
        lines.append("|:-:|:---:|---:|")
        for m in recipe['materials']:
            lines.append(f"| {m['name']} | {m['quantity']} | {m['buildable']} |")
    return "\n".join(lines)


def is_encryption_skill(skill_name):
    return any(kw in skill_name.lower() for kw in ENCRYPTION_SKILL_KEYWORDS)


def format_invention_report(sde: SDE, target_type_id: int, target_name: str, skill_level: int):
    lines = []
    item_t = sde.types_by_id.get(target_type_id)
    if not item_t:
        raise ValueError(f"Item not found: {target_name}")
    item_name = item_t.get('typeName', target_name)
    item_type_id = target_type_id
    group = sde.get_group_hierarchy(target_type_id)
    market_group_path = sde.get_market_group_path(target_type_id)
    tech_level = sde.get_tech_level(target_type_id)
    lines.append(f"ITEM: {item_name} ({item_type_id})")
    lines.append(f"Group: {group[0]} > {group[1]}")
    if market_group_path:
        lines.append(f"Market Group: {' > '.join(market_group_path)}")
    else:
        lines.append("Market Group: None")
    lines.append(f"Tech Level: {tech_level}")
    lines.append("")
    lines.append("Invention:")
    inv_data = sde.get_invention_data(target_type_id)
    if not inv_data:
        raise ValueError(f"Item cannot be invented via Invention: {target_name}")
    if tech_level == "Tech II":
        lines.append(f"Base: {inv_data['blueprint_name']} ({inv_data['blueprint_id']})")
    datacore_names = [dc['name'] for dc in inv_data['datacores']]
    lines.append(f"Datacores: {', '.join(datacore_names)}")
    lines.append(f"Required Per Run: {inv_data['required_per_run']}")
    skill_names = [s['name'] for s in inv_data['skills']]
    lines.append(f"Skills: {', '.join(skill_names)}")
    lines.append(f"Time: {inv_data['time']}")
    base_prob = inv_data['base_prob']
    science_skills = [s for s in inv_data['skills'] if not is_encryption_skill(s['name'])]
    encryption_skills = [s for s in inv_data['skills'] if is_encryption_skill(s['name'])]
    if tech_level == "Tech III":
        relic_info = sde.get_tech3_relic_type(target_type_id)
        if relic_info:
            relic_name, relic_chance, relic_runs = relic_info
            base_runs = relic_runs
            lines.append("Probabilities:")
            lines.append("| Invented From                | Decryptor                       | Probability | Runs | ME | TE |")
            lines.append("|------------------------------|---------------------------------|-------------|------|----|----|")
            qual_order = ["Intact", "Malfunctioning", "Wrecked"]
            for qual in qual_order:
                base_info = TECH_III_RELIC_BASELINES[qual]
                if qual == "Intact":
                    relic_prefix = "Intact"
                elif qual == "Malfunctioning":
                    relic_prefix = "Malfunctioning"
                else:
                    relic_prefix = "Wrecked"
                rel_name = None
                for r_name in [f"{relic_prefix} Hull Section", f"{relic_prefix} Thruster Sections"]:
                    if r_name in relic_name:
                        rel_name = r_name
                        break
                if not rel_name:
                    rel_name = relic_name
                prob_multiplier_skill = (skill_level * len(science_skills)) / 30.0 + skill_level / 40.0
                for decryptor in DECRYPTORS + [NONE_DECRYPTOR]:
                    b_prob = base_info['base_chance']
                    success = b_prob * (1 + prob_multiplier_skill)
                    success = success * (1 + decryptor['prob_mult'] / 100.0)
                    success = max(0, min(1, success))
                    prob_pct = success * 100
                    base_me = 2
                    base_te = 4
                    final_runs = base_info['base_runs'] + decryptor['runs']
                    final_me = base_me + decryptor['me']
                    final_te = base_te + decryptor['te']
                    lines.append(f"| {rel_name:<28} | {decryptor['name']:<31} | {prob_pct:>10.2f} | {final_runs:>4} | {final_me:>3} | {final_te:>3} |")
    else:
        skill_multiplier = (skill_level * len(science_skills)) / 30.0 + skill_level / 40.0
        lines.append("Probabilities:")
        lines.append("| Decryptor                       | Probability | Runs | ME | TE |")
        lines.append("|---------------------------------|-------------|------|----|----|")
        for decryptor in DECRYPTORS + [NONE_DECRYPTOR]:
            success = base_prob * (1 + skill_multiplier)
            success = success * (1 + decryptor['prob_mult'] / 100.0)
            success = max(0, min(1, success))
            prob_pct = success * 100
            base_me = 2
            base_te = 4
            base_runs = 10
            for p in sde.products:
                if (p['typeID'] == inv_data['blueprint_id'] and
                    p['activityID'] == INVENTION_ACTIVITY_ID and
                    p['productTypeID'] == target_type_id):
                    base_runs = p['quantity']
                    break
            final_runs = base_runs + decryptor['runs']
            final_me = base_me + decryptor['me']
            final_te = base_te + decryptor['te']
            lines.append(f"| {decryptor['name']:<31} | {prob_pct:>10.2f} | {final_runs:>4} | {final_me:>3} | {final_te:>3} |")
    return "\n".join(lines)


def build_dependency_tree(sde: SDE, target_type_id: int) -> dict:
    """
    Build a dependency tree for the given product.
    Returns a dict mapping type_id to its recipe info.
    """
    tree = {}
    queue = deque([target_type_id])
    visited = set()

    while queue:
        current_id = queue.popleft()
        if current_id in visited:
            continue
        visited.add(current_id)

        # Check if buildable
        blueprint_id = sde.get_blueprint_for_product(current_id)
        if blueprint_id is None:
            # Not buildable, skip
            continue

        # Get recipe
        t = sde.types_by_id.get(current_id, {})
        name = t.get('typeName', f"Unknown ({current_id})")
        try:
            recipe = get_recipe(sde, current_id, name, 0, 0)
        except ValueError:
            continue

        tree[current_id] = recipe

        # Add dependencies to queue
        for mat in recipe['materials']:
            mat_type_id = mat['typeID']
            if mat['buildable'] == 'Yes' and mat_type_id not in visited and mat_type_id not in queue:
                queue.append(mat_type_id)

    return tree


def handle_plan_command(sde: SDE, build_string: str, config_path: str, output_dir: str, sde_dir: str):
    """Handle the 'plan' command - creates a build plan CSV files."""
    config = load_yaml(config_path)
    te_bonus = config.get('te_bonus', 0)
    invention_skill = config.get('invention_skill', 5)
    split_days = config.get('split_days', 3.0)
    component_bpo_defaults = config.get('component_bpo_defaults', {})
    default_me = component_bpo_defaults.get('me', 10)
    default_te = component_bpo_defaults.get('te', 20)

    # Validate split_days
    if not (0 < split_days <= 30):
        raise ValueError(f"split_days must be between 0 and 30, got {split_days}")

    parts = build_string.strip().split()

    # Check for --build-all flag
    build_all = False
    if '--build-all' in parts:
        build_all = True
        parts = [p for p in parts if p != '--build-all']

    if 'x' in parts:
        x_idx = parts.index('x')
        num_jobs = int(parts[x_idx + 1])
        parts = parts[:x_idx]
    else:
        num_jobs = 1

    item_name = ' '.join(parts[0:1])
    target_type_id = find_target(sde, item_name)

    is_invention = False
    is_reverse_engineering = False
    me = 0
    te = 0
    runs_per_job = 1

    original_parts = build_string.strip().split()
    x_idx = -1
    for i, p in enumerate(original_parts):
        if p == 'x':
            x_idx = i
            break

    if x_idx >= 0:
        before_x = original_parts[:x_idx]
    else:
        before_x = original_parts

    after_item = before_x[1:]

    if len(after_item) == 1:
        runs_per_job = int(after_item[0])
        me = 0
        te = 0
    elif len(after_item) == 2:
        runs_per_job = int(after_item[0])
        potential2 = after_item[1]
        if potential2 == 'None':
            is_invention = True
        elif any(d['name'].lower() in potential2.lower() for d in DECRYPTORS):
            is_invention = True
        elif any(r in potential2 for r in ['Intact', 'Malfunctioning', 'Wrecked']):
            is_reverse_engineering = True
        else:
            is_invention = True
    elif len(after_item) >= 3:
        runs_per_job = int(after_item[0])
        if after_item[1].isdigit() or (after_item[1].startswith('-') and after_item[1][1:].isdigit()):
            me = int(after_item[1])
            te = int(after_item[2])
        else:
            if len(after_item) >= 4:
                is_reverse_engineering = True
            else:
                is_invention = True

    if len(after_item) == 3:
        if not after_item[1].isdigit() and not (after_item[1].startswith('-') and after_item[1][1:].isdigit()):
            if any(r in after_item[2] for r in ['Intact', 'Malfunctioning', 'Wrecked']):
                is_reverse_engineering = True
                is_invention = False
            else:
                is_invention = True
                is_reverse_engineering = False

    if build_all:
        # Build all buildable items in dependency tree
        dependency_tree = build_dependency_tree(sde, target_type_id)
        all_jobs = []
        all_materials = defaultdict(int)

        for item_type_id, recipe in dependency_tree.items():
            name = recipe['item_name']
            blueprint_id = recipe['blueprint_id']

            # Determine ME/TE settings
            # Check if this is a T1 Capital ship or component
            if sde.is_t1_capital(item_type_id):
                item_me = 0
                item_te = 0
            else:
                item_me = me if me > 0 else default_me
                item_te = te if te > 0 else default_te

            # Get max runs
            if recipe['activity'] == "Reactions":
                max_runs = 10  # Reactions typically have 10 runs
            else:
                max_runs = 10  # Default for manufacturing blueprints

            # Calculate total runs needed
            total_runs_needed = runs_per_job * num_jobs

            # For build_all, we need to calculate based on dependencies
            # Each item produces recipe['output_quantity'] per run
            # So we need enough runs to produce what's required

            # For simplicity, assume we're building all items in the tree
            # Calculate number of blueprint runs needed
            if recipe['output_quantity'] > 0:
                num_blueprint_runs = math.ceil(total_runs_needed / recipe['output_quantity'])
            else:
                num_blueprint_runs = total_runs_needed

            runs_per_job_actual = max_runs

            # Get run time
            time_sec = sde.get_activity_time(blueprint_id, 1)
            if time_sec is None:
                time_sec = 3600  # Default 1 hour

            # Apply TE
            bp_te_factor = 1 - (item_te / 100.0)
            te_bonus_factor = 1 - (te_bonus / 100.0)
            full_te_factor = bp_te_factor * te_bonus_factor
            run_time_sec = time_sec * full_te_factor
            run_time_min = math.ceil(run_time_sec / 60)
            days_per_run = run_time_min / 1440.0

            # Get materials
            materials = sde.get_materials(blueprint_id, 1)
            if recipe['activity'] == "Reactions":
                materials = sde.get_materials(blueprint_id, 11)

            material_dict = {}
            for m in materials:
                mat_type_id = m['materialTypeID']
                mat_t = sde.types_by_id.get(mat_type_id, {})
                mat_name = mat_t.get('typeName', f"Unknown ({mat_type_id})")
                base_qty = m['quantity']
                if recipe['activity'] == "Manufacturing":
                    reduced_qty = base_qty * (1 - item_me / 100.0)
                    adjusted_qty = math.ceil(max(1, reduced_qty))
                else:
                    adjusted_qty = base_qty
                if mat_name not in material_dict:
                    material_dict[mat_name] = 0
                material_dict[mat_name] += adjusted_qty

            # Total materials needed
            total_materials = {}
            for mat_name, qty_per_run in material_dict.items():
                total_materials[mat_name] = qty_per_run * num_blueprint_runs

            # Split jobs if needed
            max_threshold = min(30, split_days)
            if days_per_run > 0 and days_per_run * max_runs > max_threshold:
                # Need to split jobs
                job_splits = split_jobs_algorithm(num_blueprint_runs * max_runs, max_threshold, days_per_run, split_days)
                # Create individual jobs
                for split_count in job_splits:
                    if split_count > 0:
                        all_jobs.append({
                            'name': name,
                            'runs': max_runs,
                            'days': f"{days_per_run:.2f}",
                            'count': max_runs
                        })
                        # Calculate materials for this split
                        split_materials = {}
                        for mat_name, total_qty in total_materials.items():
                            total_runs = num_blueprint_runs * max_runs
                            per_run = total_qty / total_runs if total_runs > 0 else 0
                            split_materials[mat_name] = per_run * split_count
                        for mat_name, qty in split_materials.items():
                            all_materials[mat_name] += math.ceil(qty)
            else:
                # No split needed
                for _ in range(num_blueprint_runs):
                    all_jobs.append({
                        'name': name,
                        'runs': runs_per_job_actual,
                        'days': f"{days_per_run:.2f}",
                        'count': max_runs
                    })
                for mat_name, qty in total_materials.items():
                    all_materials[mat_name] += qty

        # Sort jobs
        all_jobs.sort(key=lambda j: (j['name'], j['runs'], j['count'], j['days']))

        # Ensure output directory exists
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        # Write materials.csv
        materials_file = output_path / 'materials.csv'
        with open(materials_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['name', 'to_buy', 'buy_volume', 'start_amount', 'end_amount'])
            for mat_name, total_qty in sorted(all_materials.items(), key=lambda x: x[0].lower()):
                buy_volume = total_qty
                writer.writerow([mat_name, total_qty, buy_volume, 0, 0])

        # Write jobs.csv
        jobs_file = output_path / 'jobs.csv'
        with open(jobs_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['name', 'runs', 'days', 'count'])
            for job in all_jobs:
                writer.writerow([job['name'], job['runs'], job['days'], job['count']])

        return

    # Original plan command logic for single item
    if is_invention or is_reverse_engineering:
        inv_data = sde.get_invention_data(target_type_id)
        if not inv_data:
            raise ValueError(f"Cannot invent or reverse engineer: {item_name}")
        blueprint_id = inv_data['blueprint_id']
        blueprint_name = inv_data['blueprint_name']
        tech_level = inv_data['tech_level']
    else:
        blueprint_id = sde.get_blueprint_for_product(target_type_id)
        if blueprint_id is None:
            raise ValueError(f"No blueprint found for product: {item_name}")
        bp_t = sde.types_by_id.get(blueprint_id, {})
        blueprint_name = bp_t.get('typeName', f"Unknown ({blueprint_id})")
        tech_level = sde.get_tech_level(target_type_id)

    if is_invention or is_reverse_engineering:
        if is_reverse_engineering:
            relic_info = sde.get_tech3_relic_type(target_type_id)
            if relic_info:
                relic_name, relic_chance, base_runs = relic_info
            else:
                base_runs = 10
            decryptor = NONE_DECRYPTOR
            for d in DECRYPTORS:
                if d['name'] in build_string:
                    decryptor = d
                    break
            max_runs = base_runs + decryptor['runs']
        else:
            base_runs = 10
            for p in sde.products:
                if (p['typeID'] == blueprint_id and
                    p['activityID'] == INVENTION_ACTIVITY_ID and
                    p['productTypeID'] == target_type_id):
                    base_runs = p['quantity']
                    break
            decryptor = NONE_DECRYPTOR
            for d in DECRYPTORS:
                if d['name'] in build_string:
                    decryptor = d
                    break
            max_runs = base_runs + decryptor['runs']
        total_runs_needed = runs_per_job * num_jobs
        num_blueprint_runs = math.ceil(total_runs_needed / max_runs)
        runs_per_job_actual = max_runs
    else:
        max_runs = 10
        total_runs_needed = runs_per_job * num_jobs
        num_blueprint_runs = math.ceil(total_runs_needed / max_runs)
        runs_per_job_actual = max_runs

    time_sec = sde.get_activity_time(blueprint_id, 1)
    if time_sec is None:
        raise ValueError(f"No manufacturing time for blueprint {blueprint_id}")

    bp_te_factor = 1 - (te / 100.0)
    te_bonus_factor = 1 - (te_bonus / 100.0)
    full_te_factor = bp_te_factor * te_bonus_factor
    run_time_sec = time_sec * full_te_factor
    run_time_min = math.ceil(run_time_sec / 60)

    materials = sde.get_materials(blueprint_id, 1)
    material_dict = {}
    for m in materials:
        mat_type_id = m['materialTypeID']
        mat_t = sde.types_by_id.get(mat_type_id, {})
        mat_name = mat_t.get('typeName', f"Unknown ({mat_type_id})")
        base_qty = m['quantity']
        reduced_qty = base_qty * (1 - me / 100.0)
        adjusted_qty = math.ceil(max(1, reduced_qty))
        if mat_name not in material_dict:
            material_dict[mat_name] = 0
        material_dict[mat_name] += adjusted_qty

    total_materials = {}
    for mat_name, qty_per_run in material_dict.items():
        total_materials[mat_name] = qty_per_run * num_blueprint_runs

    sorted_materials = sorted(total_materials.items(), key=lambda x: x[0].lower())

    jobs = []
    for job_num in range(1, num_blueprint_runs + 1):
        jobs.append({
            'name': blueprint_name,
            'runs': runs_per_job_actual,
            'days': run_time_min / 1440.0,
            'count': max_runs
        })

    jobs.sort(key=lambda j: (j['name'], j['runs'], j['count'], j['days']))

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    materials_file = output_path / 'materials.csv'
    with open(materials_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['name', 'to_buy', 'buy_volume', 'start_amount', 'end_amount'])
        for mat_name, total_qty in sorted_materials:
            buy_volume = total_qty
            writer.writerow([mat_name, total_qty, buy_volume, 0, 0])

    jobs_file = output_path / 'jobs.csv'
    with open(jobs_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['name', 'runs', 'days', 'count'])
        for job in jobs:
            writer.writerow([job['name'], job['runs'], f"{job['days']:.2f}", job['count']])


def main():
    parser = argparse.ArgumentParser(
        description="EVE Online industry recipe generator",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    recipe_parser = subparsers.add_parser("recipe", help="Generate recipe for a product or blueprint")
    recipe_parser.add_argument("name", help="Product or Blueprint name (exact, case-sensitive)")
    recipe_parser.add_argument("--sde", required=True, help="Path to SDE directory")
    recipe_parser.add_argument("--material-efficiency", "-me", type=int, choices=range(0, 11), default=0, dest="me",
                               help="Material Efficiency level (0-10, default: 0)")
    recipe_parser.add_argument("--time-efficiency", "-te", type=int, choices=range(0, 21, 2), default=0, dest="te",
                               help="Time Efficiency level (0-20, even only, default: 0)")
    recipe_parser.add_argument("--display-waste", "-waste", action="store_true", dest="display_waste",
                               help="Display waste due to ME not being max")

    invention_parser = subparsers.add_parser("invention", help="Generate invention report for a Tech II/III product")
    invention_parser.add_argument("name", help="Product or Blueprint name (exact, case-sensitive)")
    invention_parser.add_argument("--sde", required=True, help="Path to SDE directory")
    invention_parser.add_argument("--skill", type=int, choices=range(1, 6), default=5,
                                  help="Skill level for science and encryption skills (default: 5)")

    plan_parser = subparsers.add_parser("plan", help="Create a build plan for a given item")
    plan_parser.add_argument("build_string", help="Build string (see documentation for formats)")
    plan_parser.add_argument("--config", required=True, help="YAML build config file")
    plan_parser.add_argument("--sde", required=True, help="Path to SDE directory")
    plan_parser.add_argument("--output", required=True, help="Output directory for CSV files")

    args = parser.parse_args()

    sde = SDE(args.sde)

    if args.command == "recipe":
        target_type_id = find_target(sde, args.name)
        recipe = get_recipe(sde, target_type_id, args.name, args.me, args.te, args.display_waste)
        output = format_recipe(recipe, args.display_waste)
    elif args.command == "invention":
        target_type_id = find_target(sde, args.name)
        output = format_invention_report(sde, target_type_id, args.name, args.skill)
    elif args.command == "plan":
        handle_plan_command(sde, args.build_string, args.config, args.output, args.sde)
        return

    print(output)


if __name__ == "__main__":
    main()
