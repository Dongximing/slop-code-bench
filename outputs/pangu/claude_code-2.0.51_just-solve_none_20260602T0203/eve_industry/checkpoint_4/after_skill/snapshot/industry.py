#!/usr/bin/env python3
"""
EVE Online Industry Recipe Generator
Parses Static Data Export (SDE) and emits recipe reports for products or blueprints.
"""

import argparse
import bz2
import csv
import math
from collections import defaultdict
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

        # Load invMetaTypes (Tech II/III detection)
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
        """Get time in seconds for a type/activity. Returns None if not found."""
        entry = self.activities.get((type_id, activity_id))
        if entry:
            return int(entry['time'])
        return None

    def get_products(self, type_id, activity_id):
        """Get all products for a blueprint (typeID is blueprint ID)."""
        return [p for p in self.products if p['typeID'] == type_id and p['activityID'] == activity_id]

    def get_materials(self, type_id, activity_id):
        """Get all materials for a blueprint."""
        return [m for m in self.materials if m['typeID'] == type_id and m['activityID'] == activity_id]

    def get_blueprint_for_product(self, product_type_id):
        """Find the blueprint typeID that produces the given product."""
        for p in self.products:
            if p['productTypeID'] == product_type_id and p['activityID'] in (1, 11):
                return p['typeID']
        return None

    def is_buildable(self, type_id):
        """Check if a type can be produced via industry or reactions."""
        for p in self.products:
            if p['productTypeID'] == type_id and p['activityID'] in (1, 11):
                return True
        return False

    def get_tech_level(self, type_id):
        """Determine tech level: Tech I, II, or III."""
        meta = self.meta_types_by_type.get(type_id)
        if not meta:
            return "Tech I"
        meta_group_id = int(meta['metaGroupID'])
        if meta_group_id == 2:  # Tech II
            return "Tech II"
        elif meta_group_id == 14:  # Tech III
            return "Tech III"
        return "Tech I"

    def get_volume(self, type_id, group_id):
        """Get packaged volume. For ships, use ship_volumes.yaml; otherwise use invTypes.volume."""
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
        """Return (Category Name, Group Name) hierarchy."""
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
        """Check if item uses ship packaged volume."""
        g = self.groups_by_id.get(group_id, {})
        group_name = g.get('groupName', '')
        return group_name in self.ship_volumes

    def get_market_group_path(self, type_id):
        """Return full market group path as list from root to leaf, or None."""
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
        """
        Get invention data for a target product.
        Returns dict with invention details or None if not inventable.
        """
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
        """Get skills required for invention on a blueprint."""
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
        """
        For Tech III items, determine the relic type used for invention.
        Returns (relic_name, base_chance, base_runs) or None.
        """
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

        # Determine relic quality
        if 'Intact' in relic_name:
            qual = "Intact"
        elif 'Malfunctioning' in relic_name:
            qual = "Malfunctioning"
        elif 'Wrecked' in relic_name:
            qual = "Wrecked"
        else:
            qual = "Wrecked"  # Default

        baseline = TECH_III_RELIC_BASELINES.get(qual, TECH_III_RELIC_BASELINES['Wrecked'])
        return (relic_name, baseline['base_chance'], baseline['base_runs'])


def find_target(sde: SDE, name: str) -> int:
    """Find typeID by exact typeName (case-sensitive). Raises if not found."""
    if name in sde.types_by_name:
        return int(sde.types_by_name[name]['typeID'])
    raise ValueError(f"Unknown product or blueprint: {name}")


def get_recipe(sde: SDE, target_type_id: int, target_name: str, me: int = 0, te: int = 0, display_waste: bool = False) -> dict:
    """Extract recipe for a product or blueprint."""

    blueprint_id = sde.get_blueprint_for_product(target_type_id)
    if blueprint_id is not None:
        bp_id = blueprint_id
        is_product = True
    else:
        bp_id = target_type_id
        is_product = False

    manufact_mat = sde.get_materials(bp_id, 1)
    reaction_mat = sde.get_materials(bp_id, 11)

    if manufact_mat:
        activity_id = 1
        activity_name = "Manufacturing"
    elif reaction_mat:
        activity_id = 11
        activity_name = "Reactions"
    else:
        raise ValueError(f"No industry activity found for: {target_name}")

    products = sde.get_products(bp_id, activity_id)
    if not products:
        raise ValueError(f"No products defined for blueprint: bp_id={bp_id}")

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

    time_sec = sde.get_activity_time(bp_id, activity_id)
    if time_sec is None:
        raise ValueError(f"No activity time for blueprint {bp_id}, activity {activity_id}")

    # Apply Time Efficiency reduction
    # TE must be an even integer in [0, 20], reduces time by TE%
    if activity_id == 1:  # Manufacturing
        run_time_sec = time_sec * (1 - te / 100)
    else:  # Reactions are not affected by TE
        run_time_sec = time_sec
    run_time_min = math.ceil(run_time_sec / 60)

    materials = sde.get_materials(bp_id, activity_id)
    material_list = []
    for m in materials:
        mat_type_id = m['materialTypeID']
        mat_t = sde.types_by_id.get(mat_type_id, {})
        mat_name = mat_t.get('typeName', f"Unknown ({mat_type_id})")
        buildable = sde.is_buildable(mat_type_id)

        # Calculate quantity with Material Efficiency for Manufacturing only
        # ME formula: Amount needed = ceil(max(1, quantity required per run * (1 - ME/100)))
        # Reactions are not impacted by ME
        if activity_id == 1:  # Manufacturing
            base_qty = m['quantity']
            # ME reduces material usage: (1 - ME/100)
            reduced_qty = base_qty * (1 - me / 100)
            # We always need at least 1 of each material
            adjusted_qty = math.ceil(max(1, reduced_qty))
        else:
            # For reactions, use base quantity without ME adjustment
            adjusted_qty = m['quantity']

        material_entry = {
            'typeID': mat_type_id,
            'name': mat_name,
            'quantity': adjusted_qty,
            'buildable': 'Yes' if buildable else 'No'
        }

        # Calculate and include waste if requested
        # Waste represents extra materials used because ME is not at max (ME=10)
        # Example: ME=5 gives 24700 Isogen, ME=10 gives 23400, so waste = 24700 - 23400 = 1300
        if display_waste and activity_id == 1:
            max_me_qty = math.ceil(max(1, base_qty * (1 - 10 / 100)))  # ME=10 gives best efficiency
            waste = adjusted_qty - max_me_qty
            material_entry['waste'] = waste

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
        'materials': sorted(material_list, key=lambda x: x['name'].lower()),
    }


def format_recipe(recipe, display_waste=False):
    """Format recipe as the canonical output block."""
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
    """Check if a skill is an encryption skill."""
    return any(kw in skill_name.lower() for kw in ENCRYPTION_SKILL_KEYWORDS)


def format_invention_report(sde: SDE, target_type_id: int, target_name: str, skill_level: int):
    """Format the invention report as canonical output block."""
    lines = []

    # Get basic item info
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

    # Get invention data
    inv_data = sde.get_invention_data(target_type_id)
    if not inv_data:
        raise ValueError(f"Item cannot be invented via Invention: {target_name}")

    # For Tech II, show base blueprint
    if tech_level == "Tech II":
        lines.append(f"Base: {inv_data['blueprint_name']} ({inv_data['blueprint_id']})")

    # Format datacores
    datacore_names = [dc['name'] for dc in inv_data['datacores']]
    lines.append(f"Datacores: {', '.join(datacore_names)}")

    # Required per run
    lines.append(f"Required Per Run: {inv_data['required_per_run']}")

    # Skills
    skill_names = [s['name'] for s in inv_data['skills']]
    lines.append(f"Skills: {', '.join(skill_names)}")

    # Time
    lines.append(f"Time: {inv_data['time']}")

    # Calculate probabilities
    base_prob = inv_data['base_prob']

    # Separate science and encryption skills
    science_skills = [s for s in inv_data['skills'] if not is_encryption_skill(s['name'])]
    encryption_skills = [s for s in inv_data['skills'] if is_encryption_skill(s['name'])]

    # For Tech III, we need special handling
    if tech_level == "Tech III":
        # Tech III uses relic baselines
        relic_info = sde.get_tech3_relic_type(target_type_id)
        if relic_info:
            relic_name, relic_chance, relic_runs = relic_info

            # Override base runs from relic table
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

                    # Apply skill multiplier
                    success = b_prob * (1 + prob_multiplier_skill)

                    # Apply decryptor multiplier
                    success = success * (1 + decryptor['prob_mult'] / 100.0)

                    # Clamp to [0, 1]
                    success = max(0, min(1, success))

                    # Convert to percentage
                    prob_pct = success * 100

                    # Calculate runs/ME/TE with modifiers
                    base_me = 2
                    base_te = 4
                    final_runs = base_info['base_runs'] + decryptor['runs']
                    final_me = base_me + decryptor['me']
                    final_te = base_te + decryptor['te']

                    lines.append(f"| {rel_name:<28} | {decryptor['name']:<31} | {prob_pct:>10.2f} | {final_runs:>4} | {final_me:>3} | {final_te:>3} |")

    else:
        # Tech II (and potentially other tech levels)
        # Use the base probability from industryActivityProbabilities

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

            # Get base runs from SDE (invention product quantity)
            base_runs = 10  # Default from example
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


def handle_plan_command(sde: SDE, build_string: str, config_path: str, output_dir: str, sde_dir: str):
    """Handle the 'plan' command - creates a build plan CSV files."""

    # Load config
    config = load_yaml(config_path)
    te_bonus = config.get('te_bonus', 0)
    invention_skill = config.get('invention_skill', 5)

    # Parse build string
    parts = build_string.strip().split()

    if 'x' in parts:
        # Has x{Number} suffix
        x_idx = parts.index('x')
        num_jobs = int(parts[x_idx + 1])
        parts = parts[:x_idx]
    else:
        num_jobs = 1

    item_name = ' '.join(parts[0:1])
    target_type_id = find_target(sde, item_name)

    # Determine mode and extract params
    is_invention = False
    is_reverse_engineering = False
    me = 0
    te = 0
    runs_per_job = 1

    # Count parts after item name (skip x suffix already removed)
    # Format 1 (Item): {Item name} {Runs Per Job} x{Number of Jobs}
    # Format 2 (Normal): {Item name} {Runs Per Job} {ME} {TE} x{Number of Jobs}
    # Format 3 (Invention): {Item name} {RunsPerJob} {DecryptorName|None} x{NumJobs}
    # Format 4 (Reverse Engineering): {Item name} {RunsPerJob} {DecryptorName|None} {RelicName} x{NumJobs}

    # Rebuild parts for analysis (item_name + remaining parts from original)
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

    # before_x now is: [item_name, ...]
    # Count args after item_name

    # Check if item name has spaces - use first word only for now
    # Since item names don't have spaces in EVE, this should be fine
    after_item = before_x[1:]  # Skip item name

    # Determine format based on number of args after item
    if len(after_item) == 1:
        # Format 1: Item format (ME=0, TE=0), Runs Per Job is the value
        runs_per_job = int(after_item[0])
        me = 0
        te = 0
    elif len(after_item) == 2:
        # Could be:
        # - Format 2 (Normal): runs me te  => but 2 values isn't enough
        # - Format 3 (Invention): runs decryptor
        # - Format 4 (Reverse Engineering): runs decryptor relic => 3 values needed
        # Let's check for known decryptor names or relic names
        potential = after_item[0]
        potential2 = after_item[1] if len(after_item) > 1 else None

        runs_per_job = int(potential)

        if potential2 == 'None':
            is_invention = True
        elif any(d['name'].lower() in potential2.lower() for d in DECRYPTORS):
            is_invention = True
        elif any(r in potential2 for r in ['Intact', 'Malfunctioning', 'Wrecked']):
            is_reverse_engineering = True
        else:
            # Assume Normal format: runs me te -> but only 2 values after item means
            # this is not the normal format (normal needs 4: runs, me, te)
            # This is ambiguous, but probably invention with None decryptor
            is_invention = True
    elif len(after_item) >= 3:
        # Format 2 (Normal) or Format 3/4
        runs_per_job = int(after_item[0])

        # Check if first value after runs is a number (ME) or a string (decryptor/relic)
        if after_item[1].isdigit() or (after_item[1].startswith('-') and after_item[1][1:].isdigit()):
            # Normal format: runs me te
            me = int(after_item[1])
            te = int(after_item[2])
        else:
            # Invention or RE format
            if len(after_item) >= 4:
                is_reverse_engineering = True
            else:
                is_invention = True

    # If we determined invention is possible, but we need to check if this is RE
    # based on 3 args: runs decryptor relic
    if len(after_item) == 3:
        if not after_item[1].isdigit() and not (after_item[1].startswith('-') and after_item[1][1:].isdigit()):
            # String instead of number - could be invention (decryptor) or RE
            # Since RE has relic as 3rd arg, and invention only has 2 args, this is RE if 3 args
            if any(r in after_item[2] for r in ['Intact', 'Malfunctioning', 'Wrecked']):
                is_reverse_engineering = True
                is_invention = False
            else:
                is_invention = True
                is_reverse_engineering = False

    # Get blueprint info
    if is_invention or is_reverse_engineering:
        inv_data = sde.get_invention_data(target_type_id)
        if not inv_data:
            raise ValueError(f"Cannot invent or reverse engineer: {item_name}")

        blueprint_id = inv_data['blueprint_id']
        blueprint_name = inv_data['blueprint_name']
        tech_level = inv_data['tech_level']
    else:
        # Normal manufacturing
        blueprint_id = sde.get_blueprint_for_product(target_type_id)
        if blueprint_id is None:
            raise ValueError(f"No blueprint found for product: {item_name}")
        bp_t = sde.types_by_id.get(blueprint_id, {})
        blueprint_name = bp_t.get('typeName', f"Unknown ({blueprint_id})")
        tech_level = sde.get_tech_level(target_type_id)

    # Apply TE bonus for manufacturing
    if is_invention or is_reverse_engineering:
        # For invented/RE blueprints, max runs apply
        # Need to figure out effective max runs

        if is_reverse_engineering:
            relic_info = sde.get_tech3_relic_type(target_type_id)
            if relic_info:
                relic_name, relic_chance, base_runs = relic_info
            else:
                base_runs = 10  # default fallback

            # Apply decryptor bonus if present
            decryptor = NONE_DECRYPTOR
            for d in DECRYPTORS:
                if d['name'] in build_string:
                    decryptor = d
                    break
                # Check decryptor name match
                for p in build_string.split():
                    if d['name'] in p:
                        decryptor = d
                        break

            max_runs = base_runs + decryptor['runs']
        else:
            # Invention
            # Get max runs from SDE
            base_runs = 10
            for p in sde.products:
                if (p['typeID'] == blueprint_id and
                    p['activityID'] == INVENTION_ACTIVITY_ID and
                    p['productTypeID'] == target_type_id):
                    base_runs = p['quantity']
                    break

            # Apply decryptor bonus
            decryptor = NONE_DECRYPTOR
            for d in DECRYPTORS:
                if d['name'] in build_string:
                    decryptor = d
                    break
                # Check partial match
                for p in build_string.split():
                    if d['name'] in p:
                        decryptor = d
                        break

            max_runs = base_runs + decryptor['runs']

        # Calculate number of blueprint runs needed
        total_runs_needed = runs_per_job * num_jobs
        num_blueprint_runs = math.ceil(total_runs_needed / max_runs)

        # Effective runs per "job" (blueprint use) is max_runs, but spread across num_blueprint_runs
        runs_per_job_actual = max_runs

    else:
        # Normal manufacturing with ME/TE
        max_runs = 10  # Tech I blueprints have 10 runs (assuming standard)
        total_runs_needed = runs_per_job * num_jobs
        num_blueprint_runs = math.ceil(total_runs_needed / max_runs)
        runs_per_job_actual = max_runs

    # Get run time for the blueprint
    time_sec = sde.get_activity_time(blueprint_id, 1)
    if time_sec is None:
        raise ValueError(f"No manufacturing time for blueprint {blueprint_id}")

    # Apply TE from blueprint and TE bonus
    # BP TE: ME/TE from blueprint: TE is already in the skill: TE bonus from config is applied on top
    # Full TE factor: (1 - BP_TE) * (1 - TE_bonus/100)
    # BP_TE value is skill bonus: te (from params) is percentage
    bp_te_factor = 1 - (te / 100.0)
    te_bonus_factor = 1 - (te_bonus / 100.0)
    full_te_factor = bp_te_factor * te_bonus_factor

    run_time_sec = time_sec * full_te_factor
    run_time_min = math.ceil(run_time_sec / 60)

    # Total time = num_blueprint_runs * run_time_min (per run)
    # But each blueprint can have multiple runs? No, each run produces max_runs items

    # Get materials for the blueprint
    materials = sde.get_materials(blueprint_id, 1)

    material_dict = {}
    for m in materials:
        mat_type_id = m['materialTypeID']
        mat_t = sde.types_by_id.get(mat_type_id, {})
        mat_name = mat_t.get('typeName', f"Unknown ({mat_type_id})")
        base_qty = m['quantity']

        # Apply ME: quantity = ceil(base * (1 - ME/100))
        reduced_qty = base_qty * (1 - me / 100.0)
        adjusted_qty = math.ceil(max(1, reduced_qty))

        if mat_name not in material_dict:
            material_dict[mat_name] = 0
        material_dict[mat_name] += adjusted_qty

    # Total materials needed = sum over all blueprint runs
    total_materials = {}
    for mat_name, qty_per_run in material_dict.items():
        total_materials[mat_name] = qty_per_run * num_blueprint_runs

    # Sort materials by name
    sorted_materials = sorted(total_materials.items(), key=lambda x: x[0].lower())

    # Prepare jobs
    # Each job is one use of the blueprint, producing max_runs items per run
    jobs = []
    for job_num in range(1, num_blueprint_runs + 1):
        jobs.append({
            'name': blueprint_name,
            'runs': runs_per_job_actual,
            'days': run_time_min / 1440.0,  # Convert minutes to days
            'count': max_runs  # Each blueprint run produces max_runs items
        })

    # Sort jobs by: name > runs > count > days
    jobs.sort(key=lambda j: (j['name'], j['runs'], j['count'], j['days']))

    # Ensure output directory exists
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    # Write materials.csv
    materials_file = output_path / 'materials.csv'
    with open(materials_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['name', 'to_buy', 'buy_volume', 'start_amount', 'end_amount'])
        for mat_name, total_qty in sorted_materials:
            buy_volume = total_qty  # Default, can be refined
            writer.writerow([mat_name, total_qty, buy_volume, 0, 0])

    # Write jobs.csv
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
    target_type_id = find_target(sde, args.name)

    if args.command == "recipe":
        recipe = get_recipe(sde, target_type_id, args.name, args.me, args.te, args.display_waste)
        output = format_recipe(recipe, args.display_waste)
    elif args.command == "invention":
        output = format_invention_report(sde, target_type_id, args.name, args.skill)
    elif args.command == "plan":
        handle_plan_command(sde, args.build_string, args.config, args.output, args.sde)
        return  # Early return as handle_plan writes files directly

    print(output)


if __name__ == "__main__":
    main()
