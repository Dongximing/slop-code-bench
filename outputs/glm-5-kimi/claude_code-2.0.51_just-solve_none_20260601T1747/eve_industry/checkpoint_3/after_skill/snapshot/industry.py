#!/usr/bin/env python3
"""
EVE Online Industry Recipe Planner
Parses the Static Data Export (SDE) and emits a deterministic recipe report.
"""

import argparse
import bz2
import csv
import math
import os
import sys
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP

import yaml


def load_bz2_csv(filepath):
    """Load a bz2-compressed CSV file and return list of dicts."""
    with bz2.open(filepath, 'rt', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        return list(reader)


def load_yaml(filepath):
    """Load a YAML file."""
    with open(filepath, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


# Decryptor modifiers: (probability_multiplier, runs, ME, TE)
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


class SDEDatabase:
    """Container for all SDE data."""

    def __init__(self, sde_dir):
        self.sde_dir = sde_dir
        self._load_all()

    def _load_all(self):
        self.inv_types = self._load_csv('invTypes.csv.bz2')
        self.inv_groups = self._load_csv('invGroups.csv.bz2')
        self.inv_categories = self._load_csv('invCategories.csv.bz2')
        self.inv_market_groups = self._load_csv('invMarketGroups.csv.bz2')
        self.inv_meta_types = self._load_csv('invMetaTypes.csv.bz2')
        self.inv_meta_groups = self._load_csv('invMetaGroups.csv.bz2')

        self.industry_activity = self._load_csv('industryActivity.csv.bz2')
        self.industry_activity_products = self._load_csv('industryActivityProducts.csv.bz2')
        self.industry_activity_materials = self._load_csv('industryActivityMaterials.csv.bz2')
        self.industry_activity_skills = self._load_csv('industryActivitySkills.csv.bz2')
        self.industry_activity_probabilities = self._load_csv('industryActivityProbabilities.csv.bz2')
        self.ram_activities = self._load_csv('ramActivities.csv.bz2')

        self.ship_volumes = load_yaml(os.path.join(self.sde_dir, 'ship_volumes.yaml'))

        self._build_indexes()

    def _load_csv(self, filename):
        """Load a CSV file from the SDE directory."""
        filepath = os.path.join(self.sde_dir, filename)
        return load_bz2_csv(filepath)

    def _build_indexes(self):
        self.types_by_id = {}
        self.types_by_name = {}
        for row in self.inv_types:
            if row.get('published') == '1':
                type_id = int(row['typeID'])
                self.types_by_id[type_id] = row
                type_name = row['typeName']
                self.types_by_name[type_name] = row

        self.groups_by_id = {}
        for row in self.inv_groups:
            group_id = int(row['groupID'])
            self.groups_by_id[group_id] = row

        self.categories_by_id = {}
        for row in self.inv_categories:
            category_id = int(row['categoryID'])
            self.categories_by_id[category_id] = row

        self.market_groups_by_id = {}
        for row in self.inv_market_groups:
            market_group_id = int(row['marketGroupID'])
            self.market_groups_by_id[market_group_id] = row

        self.meta_groups_by_id = {}
        for row in self.inv_meta_groups:
            meta_group_id = int(row['metaGroupID'])
            self.meta_groups_by_id[meta_group_id] = row

        self.type_meta_group = {}
        for row in self.inv_meta_types:
            type_id = int(row['typeID'])
            meta_group_id = int(row['metaGroupID'])
            self.type_meta_group[type_id] = meta_group_id

        self.activities_by_id = {}
        for row in self.ram_activities:
            activity_id = int(row['activityID'])
            self.activities_by_id[activity_id] = row['activityName']

        self.product_to_blueprint = {}
        self.blueprint_to_products = defaultdict(list)
        for row in self.industry_activity_products:
            bp_type_id = int(row['typeID'])
            activity_id = int(row['activityID'])
            product_type_id = int(row['productTypeID'])
            quantity = int(row['quantity'])
            self.product_to_blueprint[product_type_id] = (bp_type_id, activity_id)
            self.blueprint_to_products[bp_type_id].append({
                'activity_id': activity_id,
                'product_type_id': product_type_id,
                'quantity': quantity
            })

        self.blueprint_materials = defaultdict(list)
        for row in self.industry_activity_materials:
            bp_type_id = int(row['typeID'])
            activity_id = int(row['activityID'])
            material_type_id = int(row['materialTypeID'])
            quantity = int(row['quantity'])
            self.blueprint_materials[(bp_type_id, activity_id)].append({
                'material_type_id': material_type_id,
                'quantity': quantity
            })

        self.blueprint_time = {}
        for row in self.industry_activity:
            bp_type_id = int(row['typeID'])
            activity_id = int(row['activityID'])
            time = int(row['time'])
            self.blueprint_time[(bp_type_id, activity_id)] = time

        self.blueprint_skills = defaultdict(list)
        for row in self.industry_activity_skills:
            bp_type_id = int(row['typeID'])
            activity_id = int(row['activityID'])
            skill_id = int(row['skillID'])
            level = int(row['level'])
            self.blueprint_skills[(bp_type_id, activity_id)].append({
                'skill_id': skill_id,
                'level': level
            })

        self.invention_sources = defaultdict(list)
        for row in self.industry_activity_probabilities:
            bp_type_id = int(row['typeID'])
            activity_id = int(row['activityID'])
            invented_bp_type_id = int(row['productTypeID'])
            probability = float(row['probability'])
            self.invention_sources[invented_bp_type_id].append({
                'blueprint_type_id': bp_type_id,
                'activity_id': activity_id,
                'probability': probability
            })

        self.invention_products = defaultdict(list)
        for row in self.industry_activity_probabilities:
            bp_type_id = int(row['typeID'])
            activity_id = int(row['activityID'])
            product_type_id = int(row['productTypeID'])
            probability = float(row['probability'])
            self.invention_products[(bp_type_id, activity_id)].append({
                'product_type_id': product_type_id,
                'probability': probability
            })

    def get_type_by_name(self, name):
        """Get type data by exact name match."""
        return self.types_by_name.get(name)

    def get_type_by_id(self, type_id):
        """Get type data by type ID."""
        return self.types_by_id.get(type_id)

    def get_group_and_category(self, type_id):
        """Get group and category names for a type."""
        type_data = self.types_by_id.get(type_id)
        if not type_data:
            return None, None

        group_id = int(type_data['groupID'])
        group_data = self.groups_by_id.get(group_id)
        if not group_data:
            return None, None

        group_name = group_data['groupName']

        category_id = int(group_data['categoryID'])
        category_data = self.categories_by_id.get(category_id)
        category_name = category_data['categoryName'] if category_data else None

        return category_name, group_name

    def get_market_group_path(self, type_id):
        """Get the full market group path for a type."""
        type_data = self.types_by_id.get(type_id)
        if not type_data or not type_data.get('marketGroupID'):
            return None

        market_group_id = int(type_data['marketGroupID'])
        path = []

        while market_group_id:
            mg_data = self.market_groups_by_id.get(market_group_id)
            if not mg_data:
                break
            path.append(mg_data['marketGroupName'])
            parent_id = mg_data.get('parentGroupID')
            market_group_id = int(parent_id) if parent_id and parent_id != 'None' else None

        return ' > '.join(reversed(path)) if path else None

    def get_tech_level(self, type_id):
        """Get the tech level for a type."""
        meta_group_id = self.type_meta_group.get(type_id)
        if meta_group_id:
            mg_data = self.meta_groups_by_id.get(meta_group_id)
            if mg_data:
                name = mg_data['metaGroupName']
                if name == 'Tech I':
                    return 'Tech I'
                elif name == 'Tech II':
                    return 'Tech II'
                elif name == 'Tech III':
                    return 'Tech III'
        return 'Tech I'

    def get_volume(self, type_id):
        """Get the packaged volume for a type."""
        type_data = self.types_by_id.get(type_id)
        if not type_data:
            return 0.0

        group_id = int(type_data['groupID'])
        group_data = self.groups_by_id.get(group_id)
        if group_data:
            group_name = group_data['groupName']
            if self.ship_volumes and group_name in self.ship_volumes:
                return float(self.ship_volumes[group_name])

        volume = type_data.get('volume', '0')
        return float(volume) if volume else 0.0

    def is_ship(self, type_id):
        """Check if a type is a ship (has packaged volume)."""
        type_data = self.types_by_id.get(type_id)
        if not type_data:
            return False

        group_id = int(type_data['groupID'])
        group_data = self.groups_by_id.get(group_id)
        if not group_data:
            return False

        group_name = group_data['groupName']
        return group_name in self.ship_volumes if self.ship_volumes else False

    def get_blueprint_for_product(self, product_type_id):
        """Get the blueprint type ID that produces this product."""
        return self.product_to_blueprint.get(product_type_id)

    def get_recipe_for_blueprint(self, blueprint_type_id, activity_id):
        """Get recipe details for a blueprint and activity."""
        materials = self.blueprint_materials.get((blueprint_type_id, activity_id), [])
        time = self.blueprint_time.get((blueprint_type_id, activity_id), 0)

        # Get output quantity
        products = self.blueprint_to_products.get(blueprint_type_id, [])
        output_quantity = 1
        for p in products:
            if p['activity_id'] == activity_id:
                output_quantity = p['quantity']
                break

        return {
            'materials': materials,
            'time': time,
            'output_quantity': output_quantity
        }

    def is_buildable(self, type_id):
        """Check if a type can be produced via industry (has a blueprint)."""
        return type_id in self.product_to_blueprint

    def get_activity_name(self, activity_id):
        """Get the activity name for an activity ID."""
        return self.activities_by_id.get(activity_id, 'Unknown')

    def get_invention_info(self, product_type_id):
        """Get invention information for a Tech II or Tech III product."""
        return self.invention_sources.get(product_type_id) or None

    def get_invention_runs(self, blueprint_type_id, activity_id, invented_bp_type_id):
        """Get the base number of runs for an invention product.

        For Tech II: Returns the runs from industryActivityProducts for the invented blueprint.
        For Tech III: Returns the runs from the relic (from SDE).
        """
        products = self.blueprint_to_products.get(blueprint_type_id, [])
        for p in products:
            if p['activity_id'] == activity_id and p['product_type_id'] == invented_bp_type_id:
                return p['quantity']
        return 1

    def is_encryption_skill(self, skill_id):
        """Check if a skill ID is an encryption skill."""
        return skill_id in (21790, 21791, 21792, 21793, 3408)

    def get_skill_name(self, skill_id):
        """Get the name of a skill by its type ID."""
        skill_data = self.types_by_id.get(skill_id)
        return skill_data['typeName'] if skill_data else None


def format_percentage(prob):
    """Format probability as percentage rounded to 2 decimals using round half up."""
    pct = prob * 100
    # Use Decimal for proper rounding (round half up)
    d = Decimal(str(pct))
    return float(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def calculate_invention_probability(base_prob, science_skills_sum, encryption_level, decryptor_multiplier):
    """
    Calculate invention probability.

    Formula:
    success = base * (1 + sum_science/30 + encryption/40) * (1 + decryptor_prob/100)
    """
    multiplier = 1 + (science_skills_sum / 30) + (encryption_level / 40)
    decryptor_factor = 1 + (decryptor_multiplier / 100)
    prob = base_prob * multiplier * decryptor_factor
    return max(0.0, min(1.0, prob))


def format_recipe_report(db, target_name, material_efficiency=0, time_efficiency=0, display_waste=False):
    """Generate the formatted recipe report."""
    type_data = db.get_type_by_name(target_name)

    if not type_data:
        return f"Error: Item '{target_name}' not found in SDE."

    type_id = int(type_data['typeID'])

    is_blueprint = 'Blueprint' in type_data['typeName'] and type_data['typeName'].endswith('Blueprint')

    if is_blueprint:
        products = db.blueprint_to_products.get(type_id, [])
        if not products:
            return f"Error: Blueprint '{target_name}' has no products."

        activity_id = None
        for p in products:
            if p['activity_id'] in (1, 11):
                activity_id = p['activity_id']
                break

        if activity_id is None:
            activity_id = products[0]['activity_id']

        blueprint_type_id = type_id
        product_info = None
        for p in products:
            if p['activity_id'] == activity_id:
                product_info = p
                break

        if not product_info:
            product_info = products[0]

        product_type_id = product_info['product_type_id']
        product_type_data = db.get_type_by_id(product_type_id)
        if not product_type_data:
            return f"Error: Product type {product_type_id} not found."

        actual_product_name = product_type_data['typeName']
        actual_product_id = product_type_id
    else:
        bp_info = db.get_blueprint_for_product(type_id)
        if not bp_info:
            return f"Error: No blueprint found for product '{target_name}'."

        blueprint_type_id, activity_id = bp_info
        actual_product_name = target_name
        actual_product_id = type_id

    product_type_data = db.get_type_by_id(actual_product_id)
    recipe = db.get_recipe_for_blueprint(blueprint_type_id, activity_id)
    category_name, group_name = db.get_group_and_category(actual_product_id)
    market_group_path = db.get_market_group_path(actual_product_id)
    tech_level = db.get_tech_level(actual_product_id)
    volume = db.get_volume(actual_product_id)
    activity_name = db.get_activity_name(activity_id)

    base_time = recipe['time']
    adjusted_time = math.ceil(base_time * (1 - time_efficiency / 100))
    run_time_minutes = math.ceil(adjusted_time / 60)

    is_manufacturing = (activity_id == 1)

    volume_str = f"{volume:.3f}".rstrip('0')
    if volume_str.endswith('.'):
        volume_str += '00'
    elif len(volume_str.split('.')[1]) < 2:
        volume_str += '0'

    lines = []
    lines.append(f"ITEM: {actual_product_name} ({actual_product_id})")
    lines.append(f"Group: {category_name} > {group_name}")
    lines.append(f"Market Group: {market_group_path if market_group_path else 'None'}")
    lines.append(f"Tech Level: {tech_level}")
    lines.append(f"Volume: {volume_str}")
    lines.append("")
    lines.append("Recipe:")
    lines.append(f"Activity: {activity_name}")
    lines.append(f"Output Quantity: {recipe['output_quantity']}")
    lines.append(f"Run Time: {run_time_minutes}")

    if display_waste:
        lines.append("| Item | Quantity |Waste | Buildable |")
        lines.append("|:-:|:---:|:-----:|---:|")
    else:
        lines.append("| Item | Quantity | Buildable |")
        lines.append("|:-:|:---:|---:|")

    materials = []
    for mat in recipe['materials']:
        mat_type_id = mat['material_type_id']
        mat_type_data = db.get_type_by_id(mat_type_id)
        if mat_type_data:
            mat_name = mat_type_data['typeName']
            base_quantity = mat['quantity']
            buildable = 'Yes' if db.is_buildable(mat_type_id) else 'No'

            if is_manufacturing and material_efficiency > 0:
                adjusted_quantity = math.ceil(max(1, base_quantity * (1 - material_efficiency / 100)))
                waste = base_quantity - adjusted_quantity
            else:
                adjusted_quantity = base_quantity
                waste = 0

            materials.append((mat_name, adjusted_quantity, waste, buildable))

    materials.sort(key=lambda x: x[0].lower())

    for mat_name, quantity, waste, buildable in materials:
        if display_waste:
            lines.append(f"| {mat_name} | {quantity} | {waste} | {buildable} |")
        else:
            lines.append(f"| {mat_name} | {quantity} | {buildable} |")

    return '\n'.join(lines)


def format_invention_report(db, target_name, skill_level):
    """Generate the formatted invention report for Tech II or Tech III items."""
    type_data = db.get_type_by_name(target_name)

    if not type_data:
        return f"Error: Item '{target_name}' not found in SDE."

    type_id = int(type_data['typeID'])

    is_blueprint_name = 'Blueprint' in type_data['typeName'] and type_data['typeName'].endswith('Blueprint')

    if is_blueprint_name:
        blueprint_type_id = type_id
        product_type_id = None
    else:
        bp_info = db.get_blueprint_for_product(type_id)
        if not bp_info:
            return f"Error: No blueprint found for product '{target_name}'."
        blueprint_type_id, _ = bp_info
        product_type_id = type_id

    if product_type_id:
        tech_level = db.get_tech_level(product_type_id)
        product_type_data = db.get_type_by_id(product_type_id)
        product_name = product_type_data['typeName'] if product_type_data else target_name
    else:
        products = db.blueprint_to_products.get(blueprint_type_id, [])
        mfg_product = None
        for p in products:
            if p['activity_id'] == 1:
                mfg_product = p['product_type_id']
                break
        if mfg_product:
            product_type_id = mfg_product
            product_type_data = db.get_type_by_id(product_type_id)
            product_name = product_type_data['typeName'] if product_type_data else target_name
            tech_level = db.get_tech_level(product_type_id)
        else:
            return f"Error: Blueprint '{target_name}' has no manufacturing product."

    if tech_level not in ('Tech II', 'Tech III'):
        return f"Error: '{product_name}' is not a Tech II or Tech III item."

    category_name, group_name = db.get_group_and_category(product_type_id)
    market_group_path = db.get_market_group_path(product_type_id)

    invention_sources = db.get_invention_info(blueprint_type_id)
    if not invention_sources:
        return f"Error: No invention sources found for '{product_name}'."

    is_tech3 = (tech_level == 'Tech III')

    first_source = invention_sources[0]
    base_bp_type_id = first_source['blueprint_type_id']
    activity_id = first_source['activity_id']

    materials = db.blueprint_materials.get((base_bp_type_id, activity_id), [])

    datacores = []
    required_per_run = None
    for mat in materials:
        mat_type_id = mat['material_type_id']
        mat_data = db.get_type_by_id(mat_type_id)
        if mat_data:
            mat_name = mat_data['typeName']
            group_id = int(mat_data['groupID'])
            if group_id == 333:
                datacores.append(mat_name)
                if required_per_run is None:
                    required_per_run = mat['quantity']

    datacores.sort(key=lambda x: x.lower())

    skills_data = db.blueprint_skills.get((base_bp_type_id, activity_id), [])
    skill_names = []
    science_skill_count = 0

    for skill in skills_data:
        skill_id = skill['skill_id']
        skill_name = db.get_skill_name(skill_id)
        if skill_name:
            skill_names.append(skill_name)
            if not db.is_encryption_skill(skill_id):
                science_skill_count += 1

    skill_names.sort(key=lambda x: x.lower())

    time_seconds = db.blueprint_time.get((base_bp_type_id, activity_id), 0)
    time_minutes = math.ceil(time_seconds / 60)

    probabilities_table = []

    if is_tech3:
        for source in invention_sources:
            relic_type_id = source['blueprint_type_id']
            base_prob = source['probability']

            relic_data = db.get_type_by_id(relic_type_id)
            relic_name = relic_data['typeName'] if relic_data else f"Unknown ({relic_type_id})"

            if relic_name.startswith('Intact'):
                relic_quality = 'Intact'
                base_runs = 20
            elif relic_name.startswith('Malfunctioning'):
                relic_quality = 'Malfunctioning'
                base_runs = 10
            elif relic_name.startswith('Wrecked'):
                relic_quality = 'Wrecked'
                base_runs = 3
            else:
                relic_quality = 'Unknown'
                base_runs = 1

            for decryptor_name, (prob_mult, runs_add, me_add, te_add) in DECRYPTORS.items():
                science_sum = science_skill_count * skill_level
                encryption_level = skill_level
                prob = calculate_invention_probability(base_prob, science_sum, encryption_level, prob_mult)

                final_runs = base_runs + runs_add
                final_me = 2 + me_add
                final_te = 4 + te_add

                probabilities_table.append({
                    'relic_name': relic_name,
                    'decryptor_name': decryptor_name,
                    'probability': prob,
                    'runs': final_runs,
                    'me': final_me,
                    'te': final_te,
                    'relic_quality': relic_quality
                })

        quality_order = {'Intact': 0, 'Malfunctioning': 1, 'Wrecked': 2}
        def decryptor_sort_key(name):
            if name == 'None':
                return ''
            return name.lower()
        probabilities_table.sort(key=lambda x: (quality_order.get(x['relic_quality'], 99), decryptor_sort_key(x['decryptor_name'])))
    else:
        base_prob = first_source['probability']

        base_bp_data = db.get_type_by_id(base_bp_type_id)
        base_bp_name = base_bp_data['typeName'] if base_bp_data else f"Unknown ({base_bp_type_id})"

        invention_runs = db.get_invention_runs(base_bp_type_id, activity_id, blueprint_type_id)

        for decryptor_name, (prob_mult, runs_add, me_add, te_add) in DECRYPTORS.items():
            science_sum = science_skill_count * skill_level
            encryption_level = skill_level
            prob = calculate_invention_probability(base_prob, science_sum, encryption_level, prob_mult)

            final_runs = invention_runs + runs_add
            final_me = 2 + me_add
            final_te = 4 + te_add

            probabilities_table.append({
                'decryptor_name': decryptor_name,
                'probability': prob,
                'runs': final_runs,
                'me': final_me,
                'te': final_te
            })

        def decryptor_sort_key(name):
            if name == 'None':
                return ''
            return name.lower()
        probabilities_table.sort(key=lambda x: decryptor_sort_key(x['decryptor_name']))

    lines = []
    lines.append(f"ITEM: {product_name} ({product_type_id})")
    lines.append(f"Group: {category_name} > {group_name}")
    lines.append(f"Market Group: {market_group_path if market_group_path else 'None'}")
    lines.append(f"Tech Level: {tech_level}")
    lines.append("")
    lines.append("Invention:")

    if not is_tech3:
        lines.append(f"Base: {base_bp_name} ({base_bp_type_id})")

    lines.append(f"Datacores: {', '.join(datacores)}")
    lines.append(f"Required Per Run: {required_per_run}")
    lines.append(f"Skills: {', '.join(skill_names)}")
    lines.append(f"Time: {time_minutes}")
    lines.append("Probabilities:")

    if is_tech3:
        lines.append("| Invented From                | Decryptor                       | Probability | Runs | ME | TE |")
        lines.append("|------------------------------|---------------------------------|-------------|------|----|----|")
        for row in probabilities_table:
            relic_name = row['relic_name']
            decryptor = row['decryptor_name']
            prob_pct = format_percentage(row['probability'])
            runs = row['runs']
            me = row['me']
            te = row['te']
            lines.append(f"| {relic_name:<28} | {decryptor:<31} | {prob_pct:<6.2f}     | {runs:<4} | {me:<2} | {te:<2} |")
    else:
        lines.append("| Decryptor                       | Probability | Runs | ME | TE |")
        lines.append("|---------------------------------|-------------|------|----|----|")
        for row in probabilities_table:
            decryptor = row['decryptor_name']
            prob_pct = format_percentage(row['probability'])
            runs = row['runs']
            me = row['me']
            te = row['te']
            lines.append(f"| {decryptor:<31} | {prob_pct:<6.2f}     | {runs:<4} | {me:<2} | {te:<2} |")

    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description='EVE Online Industry Recipe Planner')
    subparsers = parser.add_subparsers(dest='command', help='Available commands')

    recipe_parser = subparsers.add_parser('recipe', help='Generate recipe report for a product or blueprint')
    recipe_parser.add_argument('name', help='Exact product or blueprint name')
    recipe_parser.add_argument('--sde', required=True, help='Path to SDE directory')
    recipe_parser.add_argument('--material-efficiency', '-me', type=int, default=0,
                               choices=range(0, 11), metavar='ME',
                               help='Material efficiency (0-10, default 0)')
    recipe_parser.add_argument('--time-efficiency', '-te', type=int, default=0,
                               help='Time efficiency (even integer 0-20, default 0)')
    recipe_parser.add_argument('--display-waste', '-waste', action='store_true',
                               help='Display waste due to ME not being max')

    invention_parser = subparsers.add_parser('invention', help='Generate invention report for a Tech II or Tech III product')
    invention_parser.add_argument('name', help='Exact product or blueprint name')
    invention_parser.add_argument('--sde', required=True, help='Path to SDE directory')
    invention_parser.add_argument('--skill', type=int, default=5, choices=range(1, 6),
                                   help='Skill level for all relevant skills (1-5, default 5)')

    args = parser.parse_args()

    if args.command == 'recipe':
        if not os.path.isdir(args.sde):
            print(f"Error: SDE directory '{args.sde}' not found.", file=sys.stderr)
            sys.exit(1)

        if args.time_efficiency % 2 != 0 or args.time_efficiency < 0 or args.time_efficiency > 20:
            print(f"Error: --time-efficiency must be an even integer in range [0,20].", file=sys.stderr)
            sys.exit(1)

        try:
            db = SDEDatabase(args.sde)
            report = format_recipe_report(db, args.name,
                                          material_efficiency=args.material_efficiency,
                                          time_efficiency=args.time_efficiency,
                                          display_waste=args.display_waste)
            print(report)
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    elif args.command == 'invention':
        if not os.path.isdir(args.sde):
            print(f"Error: SDE directory '{args.sde}' not found.", file=sys.stderr)
            sys.exit(1)

        try:
            db = SDEDatabase(args.sde)
            report = format_invention_report(db, args.name, args.skill)
            print(report)
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
