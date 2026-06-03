#!/usr/bin/env python3
"""
EVE Online Industry Recipe Tool
Parses the SDE and emits a deterministic recipe report for a target product or blueprint.
"""

import argparse
import csv
import math
import os
import sys
import bz2
from io import TextIOWrapper
from collections import defaultdict


def read_bz2_csv(filepath):
    """Read a BZ2-compressed CSV file and return a list of dictionaries."""
    with bz2.open(filepath, 'rt', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        return list(reader)


def read_csv(filepath):
    """Read a plain CSV file and return a list of dictionaries."""
    with open(filepath, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        return list(reader)


def read_yaml(filepath):
    """Read a plain YAML file."""
    # Simple YAML parser for key-value pairs
    with open(filepath, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    result = {}
    for line in lines:
        line = line.strip()
        if ':' in line and not line.startswith('#'):
            key, value = line.split(':', 1)
            key = key.strip()
            value = value.strip()
            # Try to convert to float if possible
            try:
                result[key] = float(value)
            except ValueError:
                result[key] = value
    return result


class SDEParser:
    """Parses SDE data files and builds lookup structures."""

    def __init__(self, sde_dir):
        self.sde_dir = sde_dir

        # Data storage
        self.types = {}  # typeID -> {typeName, groupID, volume, published, ...}
        self.types_by_name = {}  # typeName -> typeID
        self.groups = {}  # groupID -> {groupName, categoryID, ...}
        self.categories = {}  # categoryID -> categoryName
        self.market_groups = {}  # marketGroupID -> {marketGroupName, parentGroupID, ...}
        self.meta_groups = {}  # metaGroupID -> metaGroupName
        self.meta_types = defaultdict(list)  # typeID -> [(parentTypeID, metaGroupID), ...]

        # Industry data
        self.activities = {}  # (typeID, activityID) -> {activityName, time, ...}
        self.products = defaultdict(list)  # (typeID, activityID) -> [{productTypeID, quantity}, ...]
        self.materials = defaultdict(list)  # (typeID, activityID) -> [{materialTypeID, quantity}, ...]
        self.activity_probs = {}  # (typeID, activityID) -> probability
        self.activity_skills = defaultdict(list)  # (typeID, activityID) -> [{skillID, level}, ...]
        self.activity_products_map = {}  # typeID -> activityID (manufacturing/reactions)

        # Ship volumes
        self.ship_volumes = {}

        self._parse_all()

    def _parse_all(self):
        """Parse all SDE files."""
        self._parse_inv_types()
        self._parse_inv_groups()
        self._parse_inv_categories()
        self._parse_inv_market_groups()
        self._parse_inv_meta_groups()
        self._parse_inv_meta_types()
        self._parse_industry_activity()
        self._parse_industry_activity_products()
        self._parse_industry_activity_materials()
        self._parse_industry_activity_probabilities()
        self._parse_industry_activity_skills()
        self._parse_ram_activities()
        self._parse_ship_volumes()
        self._build_activity_lookup()

    def _parse_inv_types(self):
        """Parse invTypes.csv.bz2"""
        data = read_bz2_csv(os.path.join(self.sde_dir, 'invTypes.csv.bz2'))
        for row in data:
            if int(row.get('published', 0)) != 1:
                continue
            type_id = int(row['typeID'])
            type_name = row['typeName']
            volume = row.get('volume', '0')
            group_id = int(row['groupID'])

            self.types[type_id] = {
                'typeID': type_id,
                'typeName': type_name,
                'groupID': group_id,
                'volume': volume,
                'marketGroupID': row.get('marketGroupID', ''),
            }
            self.types_by_name[type_name] = type_id

    def _parse_inv_groups(self):
        """Parse invGroups.csv.bz2"""
        data = read_bz2_csv(os.path.join(self.sde_dir, 'invGroups.csv.bz2'))
        for row in data:
            if int(row.get('published', 0)) != 1:
                continue
            group_id = int(row['groupID'])
            category_id = int(row['categoryID'])
            group_name = row['groupName']

            self.groups[group_id] = {
                'groupID': group_id,
                'groupName': group_name,
                'categoryID': category_id,
            }

    def _parse_inv_categories(self):
        """Parse invCategories.csv.bz2"""
        data = read_bz2_csv(os.path.join(self.sde_dir, 'invCategories.csv.bz2'))
        for row in data:
            if int(row.get('published', 0)) != 1:
                continue
            category_id = int(row['categoryID'])
            category_name = row['categoryName']
            self.categories[category_id] = category_name

    def _parse_inv_market_groups(self):
        """Parse invMarketGroups.csv.bz2"""
        data = read_bz2_csv(os.path.join(self.sde_dir, 'invMarketGroups.csv.bz2'))
        for row in data:
            market_group_id = row['marketGroupID']
            if not market_group_id:
                continue
            market_group_id = int(market_group_id)
            parent_id = row.get('parentGroupID', '')
            parent_id = int(parent_id) if parent_id and parent_id != 'None' else None
            market_group_name = row['marketGroupName']

            self.market_groups[market_group_id] = {
                'marketGroupID': market_group_id,
                'parentGroupID': parent_id,
                'marketGroupName': market_group_name,
            }

    def _parse_inv_meta_groups(self):
        """Parse invMetaGroups.csv.bz2"""
        data = read_bz2_csv(os.path.join(self.sde_dir, 'invMetaGroups.csv.bz2'))
        for row in data:
            # invMetaGroups.csv has no published column - all are valid
            meta_group_id = int(row['metaGroupID'])
            meta_group_name = row['metaGroupName']
            self.meta_groups[meta_group_id] = meta_group_name

    def _parse_inv_meta_types(self):
        """Parse invMetaTypes.csv.bz2"""
        data = read_bz2_csv(os.path.join(self.sde_dir, 'invMetaTypes.csv.bz2'))
        for row in data:
            type_id = int(row['typeID'])
            parent_type_id = row.get('parentTypeID', '')
            parent_type_id = int(parent_type_id) if parent_type_id and parent_type_id != 'None' else None
            meta_group_id = row.get('metaGroupID', '')
            meta_group_id = int(meta_group_id) if meta_group_id and meta_group_id != 'None' else None

            if parent_type_id:
                self.meta_types[type_id].append({
                    'parentTypeID': parent_type_id,
                    'metaGroupID': meta_group_id
                })

    def _parse_industry_activity(self):
        """Parse industryActivity.csv.bz2"""
        data = read_bz2_csv(os.path.join(self.sde_dir, 'industryActivity.csv.bz2'))
        for row in data:
            type_id = int(row['typeID'])
            activity_id = int(row['activityID'])
            time_val = int(row['time'])

            self.activities[(type_id, activity_id)] = {
                'typeID': type_id,
                'activityID': activity_id,
                'time': time_val,
            }

    def _parse_industry_activity_products(self):
        """Parse industryActivityProducts.csv.bz2"""
        data = read_bz2_csv(os.path.join(self.sde_dir, 'industryActivityProducts.csv.bz2'))
        for row in data:
            type_id = int(row['typeID'])
            activity_id = int(row['activityID'])
            product_type_id = int(row['productTypeID'])
            quantity = int(row['quantity'])

            self.products[(type_id, activity_id)].append({
                'productTypeID': product_type_id,
                'quantity': quantity,
            })

    def _parse_industry_activity_materials(self):
        """Parse industryActivityMaterials.csv.bz2"""
        data = read_bz2_csv(os.path.join(self.sde_dir, 'industryActivityMaterials.csv.bz2'))
        for row in data:
            type_id = int(row['typeID'])
            activity_id = int(row['activityID'])
            material_type_id = int(row['materialTypeID'])
            quantity = int(row['quantity'])

            self.materials[(type_id, activity_id)].append({
                'materialTypeID': material_type_id,
                'quantity': quantity,
            })

    def _parse_industry_activity_probabilities(self):
        """Parse industryActivityProbabilities.csv.bz2"""
        data = read_bz2_csv(os.path.join(self.sde_dir, 'industryActivityProbabilities.csv.bz2'))
        for row in data:
            type_id = int(row['typeID'])
            activity_id = int(row['activityID'])
            probability = float(row['probability'])
            self.activity_probs[(type_id, activity_id)] = probability

    def _parse_industry_activity_skills(self):
        """Parse industryActivitySkills.csv.bz2"""
        data = read_bz2_csv(os.path.join(self.sde_dir, 'industryActivitySkills.csv.bz2'))
        for row in data:
            type_id = int(row['typeID'])
            activity_id = int(row['activityID'])
            skill_id = int(row['skillID'])
            level = int(row['level'])
            self.activity_skills[(type_id, activity_id)].append({
                'skillID': skill_id,
                'level': level
            })

    def _parse_ram_activities(self):
        """Parse ramActivities.csv.bz2 - maps activityID to activityName"""
        data = read_bz2_csv(os.path.join(self.sde_dir, 'ramActivities.csv.bz2'))
        self.activity_names = {}
        for row in data:
            activity_id = int(row['activityID'])
            activity_name = row['activityName']
            self.activity_names[activity_id] = activity_name

    def _parse_ship_volumes(self):
        """Parse ship_volumes.yaml"""
        filepath = os.path.join(self.sde_dir, 'ship_volumes.yaml')
        if os.path.exists(filepath):
            self.ship_volumes = read_yaml(filepath)

    def _build_activity_lookup(self):
        """Build lookup from typeID to manufacturing/reactions activity"""
        for (type_id, activity_id), activity_data in self.activities.items():
            activity_name = self.activity_names.get(activity_id, '')
            if activity_name in ('Manufacturing', 'Reactions'):
                self.activity_products_map[type_id] = activity_id

        # Also build reverse lookup: product_type_id -> activity_id
        # This lets us check if a material is buildable by seeing if it's a product
        self.product_to_activity = {}
        for (type_id, activity_id), products in self.products.items():
            activity_name = self.activity_names.get(activity_id, '')
            if activity_name in ('Manufacturing', 'Reactions'):
                for product in products:
                    self.product_to_activity[product['productTypeID']] = activity_id

    def get_type_by_name(self, name):
        """Get type info by exact name."""
        type_id = self.types_by_name.get(name)
        if type_id:
            return self.types[type_id]
        return None

    def get_blueprint_from_product(self, product_type_id):
        """Find the blueprint that produces the given product type."""
        for (bp_type_id, activity_id), products in self.products.items():
            for product in products:
                if product['productTypeID'] == product_type_id:
                    return bp_type_id
        return None

    def get_product_from_blueprint(self, bp_type_id):
        """Get the product info for a blueprint."""
        activity_id = self.activity_products_map.get(bp_type_id)
        if not activity_id:
            return None, None

        products = self.products.get((bp_type_id, activity_id))
        if not products:
            return None, None

        activity_info = self.activities.get((bp_type_id, activity_id), {})
        return products[0], activity_info

    def get_invention_data(self, type_id):
        """Get invention materials, skills, probability, and time for a Tech II/Tech III product."""
        # Activity ID 8 = Invention
        activity_id = 8

        # Get invention time
        activity_info = self.activities.get((type_id, activity_id), {})
        time_val = activity_info.get('time', 0)

        # Get invention probability
        probability = self.activity_probs.get((type_id, activity_id), 0.0)

        # Get invention materials (datacores)
        materials = self.materials.get((type_id, activity_id), [])

        # Get invention skills
        skills = self.activity_skills.get((type_id, activity_id), [])

        return {
            'time': time_val,
            'probability': probability,
            'materials': materials,
            'skills': skills
        }

    def get_materials(self, type_id):
        """Get materials for manufacturing/reactions for a type."""
        activity_id = self.activity_products_map.get(type_id)
        if not activity_id:
            return []

        return self.materials.get((type_id, activity_id), [])

    def get_tech_level(self, type_id):
        """Determine tech level of an item based on meta types."""
        meta_groups = self.meta_types.get(type_id, [])
        if not meta_groups:
            return "Tech I"

        # Check the meta group
        for meta_info in meta_groups:
            meta_group_id = meta_info.get('metaGroupID')
            if meta_group_id:
                meta_group_name = self.meta_groups.get(meta_group_id, '')
                if meta_group_name in ('Technology II', 'Tech II'):
                    return "Tech II"
                elif meta_group_name in ('Technology III', 'Tech III'):
                    return "Tech III"

        return "Tech I"

    def get_group_hierarchy(self, group_id):
        """Get the full group hierarchy as a list from category to group."""
        hierarchy = []

        # Get category
        group = self.groups.get(group_id)
        if not group:
            return hierarchy

        category_id = group['categoryID']
        category_name = self.categories.get(category_id, '')
        if category_name:
            hierarchy.append(category_name)

        group_name = group['groupName']
        if group_name:
            hierarchy.append(group_name)

        return hierarchy

    def get_market_group_path(self, type_id):
        """Get the market group path for a type."""
        type_info = self.types.get(type_id)
        if not type_info:
            return None

        market_group_id = type_info.get('marketGroupID')
        if not market_group_id:
            return None
        # Convert to int if string
        if isinstance(market_group_id, str):
            try:
                market_group_id = int(market_group_id)
            except ValueError:
                return None

        # Build path from leaf to root
        path = []
        while market_group_id:
            market_group = self.market_groups.get(market_group_id)
            if not market_group:
                break
            path.insert(0, market_group['marketGroupName'])
            next_id = market_group.get('parentGroupID')
            if isinstance(next_id, str):
                try:
                    market_group_id = int(next_id)
                except ValueError:
                    break
            else:
                market_group_id = next_id

        return ' > '.join(path) if path else None

    def get_volume(self, type_id, group_id):
        """Get volume with ship adjustment."""
        type_info = self.types.get(type_id)
        if not type_info:
            return None

        group = self.groups.get(group_id, {})
        group_name = group.get('groupName', '')

        # Check if this is a ship type
        if group_name in self.ship_volumes:
            return self.ship_volumes[group_name]

        # Use regular volume
        volume_str = type_info.get('volume', '0')
        try:
            return float(volume_str)
        except ValueError:
            return 0.0

    def is_buildable(self, material_type_id):
        """Check if a material type can be produced via industry."""
        return material_type_id in self.product_to_activity


def resolve_material_quantities(sde, material_list, resolved=None):
    """Resolve materials for a product, handling quantities."""
    if resolved is None:
        resolved = defaultdict(int)

    for material in material_list:
        resolved[material['materialTypeID']] += material['quantity']

    return resolved


def format_recipe(sde, target_name):
    """Format the full recipe output for a target product or blueprint."""

    # Find the target type
    type_info = sde.get_type_by_name(target_name)
    if not type_info:
        return f"Error: Item '{target_name}' not found or not published."

    type_id = type_info['typeID']
    type_name = type_info['typeName']
    group_id = type_info['groupID']

    # Check if this is a blueprint (ends with ' Blueprint')
    is_bp = type_name.endswith(' Blueprint')

    if is_bp:
        # It's a blueprint
        bp_type_id = type_id
        product_info, activity_info = sde.get_product_from_blueprint(bp_type_id)

        if not product_info:
            return f"Error: No product found for blueprint '{target_name}'."

        product_type_id = product_info['productTypeID']
        product_info_type = sde.types.get(product_type_id, {})
        product_name = product_info_type.get('typeName', 'Unknown')

        output_quantity = product_info['quantity']
        run_time = activity_info.get('time', 0)
        run_time = math.ceil(run_time / 60.0)  # Convert seconds to minutes, rounded up
        activity_name = sde.activity_names.get(activity_info.get('activityID', 0), 'Manufacturing')

        # Display the actual product name
        display_name = product_name
        display_id = product_type_id
    else:
        # It's a product, find its blueprint
        bp_type_id = sde.get_blueprint_from_product(type_id)

        if not bp_type_id:
            return f"Error: No blueprint found for product '{target_name}'."

        bp_info = sde.types.get(bp_type_id, {})
        bp_name = bp_info.get('typeName', 'Unknown')

        product_info, activity_info = sde.get_product_from_blueprint(bp_type_id)

        if not product_info:
            return f"Error: No product info found for blueprint '{bp_name}'."

        output_quantity = product_info['quantity']
        run_time = activity_info.get('time', 0)
        run_time = math.ceil(run_time / 60.0)  # Convert seconds to minutes, rounded up
        activity_name = sde.activity_names.get(activity_info.get('activityID', 0), 'Manufacturing')

        display_name = type_name
        display_id = type_id

    # Get materials
    materials = sde.get_materials(bp_type_id)

    # Build output lines
    lines = []

    # ITEM line
    lines.append(f"ITEM: {display_name} ({display_id})")

    # Group line
    group_hierarchy = sde.get_group_hierarchy(group_id)
    lines.append(f"Group: {' > '.join(group_hierarchy)}")

    # Market Group line
    market_path = sde.get_market_group_path(display_id)
    lines.append(f"Market Group: {market_path}")

    # Tech Level
    tech_level = sde.get_tech_level(display_id)
    lines.append(f"Tech Level: {tech_level}")

    # Volume
    volume = sde.get_volume(display_id, group_id)
    lines.append(f"Volume: {volume:.2f}")

    lines.append("")
    lines.append("Recipe:")
    lines.append(f"Activity: {activity_name}")
    lines.append(f"Output Quantity: {output_quantity}")
    lines.append(f"Run Time: {run_time}")

    # Materials table
    lines.append("| Item | Quantity | Buildable |")
    lines.append("|:-:|:---:|---:|")

    # Sort materials alphabetically by item name (case-insensitive)
    materials_with_names = []
    for m in materials:
        mat_type_id = m['materialTypeID']
        mat_type_info = sde.types.get(mat_type_id, {})
        mat_name = mat_type_info.get('typeName', 'Unknown')
        mat_quantity = m['quantity']
        mat_buildable = sde.is_buildable(mat_type_id)
        materials_with_names.append((mat_name.lower(), mat_name, mat_quantity, mat_buildable))

    materials_with_names.sort(key=lambda x: x[0])

    for _, name, quantity, buildable in materials_with_names:
        buildable_str = 'Yes' if buildable else 'No'
        lines.append(f"| {name} | {quantity} | {buildable_str} |")

    return '\n'.join(lines)


# Decryptor definitions
DECRYPTORS = [
    {'name': 'None', 'prob_mult': 0, 'runs': 0, 'me': 0, 'te': 0},
    {'name': 'Accelerant Decryptor', 'prob_mult': 20, 'runs': 1, 'me': 2, 'te': 10},
    {'name': 'Attainment Decryptor', 'prob_mult': 80, 'runs': 4, 'me': -1, 'te': 4},
    {'name': 'Augmentation Decryptor', 'prob_mult': -40, 'runs': 9, 'me': -2, 'te': 2},
    {'name': 'Optimized Attainment Decryptor', 'prob_mult': 90, 'runs': 2, 'me': 1, 'te': -2},
    {'name': 'Optimized Augmentation Decryptor', 'prob_mult': -10, 'runs': 7, 'me': 2, 'te': 0},
    {'name': 'Parity Decryptor', 'prob_mult': 50, 'runs': 3, 'me': 1, 'te': -2},
    {'name': 'Process Decryptor', 'prob_mult': 10, 'runs': 0, 'me': 3, 'te': 6},
    {'name': 'Symmetry Decryptor', 'prob_mult': 0, 'runs': 2, 'me': 1, 'te': 8},
]

# Tech III relic baselines
TECH3_RELIC_BASELINES = {
    'Intact': {'base_prob': 0.26, 'base_runs': 20},
    'Malfunctioning': {'base_prob': 0.21, 'base_runs': 10},
    'Wrecked': {'base_prob': 0.14, 'base_runs': 3},
}

# Tech III relic type name patterns
TECH3_RELIC_PATTERNS = ['Intact', 'Malfunctioning', 'Wrecked']


def calculate_invention_probability(base_prob, skill_level, decryptor_mult):
    """Calculate success probability with skill level and decryptor modifier."""
    # Formula: base * (1 + (sum of required science skill levels)/30 + encryption skill / 40) * (1 + decryptor / 100)
    # Both science and encryption skills use the same --skill value
    skill_bonus = (skill_level / 30.0) + (skill_level / 40.0)
    prob = base_prob * (1 + skill_bonus) * (1 + decryptor_mult / 100.0)
    # Clamp to [0, 1]
    return max(0.0, min(1.0, prob))


def format_invention(sde, target_name, skill_level=5):
    """Format the full invention output for a target product or blueprint."""

    # Find the target type
    type_info = sde.get_type_by_name(target_name)
    if not type_info:
        return f"Error: Item '{target_name}' not found or not published."

    type_id = type_info['typeID']
    type_name = type_info['typeName']
    group_id = type_info['groupID']

    # Check if this is a blueprint (ends with ' Blueprint')
    is_bp = type_name.endswith(' Blueprint')

    if is_bp:
        # It's a blueprint
        bp_type_id = type_id
        # For Tech II, the product is the Tech II item
        product_type_id = bp_type_id
        product_name = type_name.replace(' Blueprint', '')
        display_name = product_name
        display_id = product_type_id
    else:
        # It's a product
        bp_type_id = sde.get_blueprint_from_product(type_id)
        if not bp_type_id:
            return f"Error: No blueprint found for product '{target_name}'."
        display_name = type_name
        display_id = type_id

    # Determine if Tech II or Tech III
    tech_level = sde.get_tech_level(display_id)
    if tech_level not in ('Tech II', 'Tech III'):
        return f"Error: '{target_name}' is not a Tech II or Tech III item."

    # Get invention data
    inv_data = sde.get_invention_data(display_id)
    run_time_sec = inv_data['time']
    run_time_min = math.ceil(run_time_sec / 60.0) if run_time_sec else 0

    # Get datacores (materials)
    datacores = []
    for m in inv_data['materials']:
        mat_type_info = sde.types.get(m['materialTypeID'], {})
        mat_name = mat_type_info.get('typeName', 'Unknown')
        datacores.append(mat_name)
    datacores.sort(key=str.lower)

    # Get skills
    skill_names = set()
    for skill_info in inv_data['skills']:
        skill_type_info = sde.types.get(skill_info['skillID'], {})
        skill_name = skill_type_info.get('typeName', 'Unknown')
        skill_names.add(skill_name)
    skills_sorted = sorted(skill_names, key=str.lower)

    # Get required per run (quantity for first datacore, all same)
    required_per_run = inv_data['materials'][0]['quantity'] if inv_data['materials'] else 0

    # Group and market group
    group_hierarchy = sde.get_group_hierarchy(group_id)
    market_path = sde.get_market_group_path(display_id)

    # Build output lines
    lines = []

    # ITEM line
    lines.append(f"ITEM: {display_name} ({display_id})")

    # Group line
    lines.append(f"Group: {' > '.join(group_hierarchy)}")

    # Market Group line
    lines.append(f"Market Group: {market_path}")

    # Tech Level
    lines.append(f"Tech Level: {tech_level}")

    lines.append("")
    lines.append("Invention:")

    if tech_level == 'Tech II':
        # Tech II specific
        # Find base T1 blueprint
        # For Tech II, the base is the T1 product that the Tech II Blueprint refers back to
        base_bp_id = sde.get_blueprint_from_product(display_id)
        bp_info = sde.types.get(base_bp_id, {})
        bp_name = bp_info.get('typeName', 'Unknown')
        lines.append(f"Base: {bp_name} ({base_bp_id})")

    # Datacores (always)
    lines.append(f"Datacores: {', '.join(datacores)}")

    # Required Per Run
    lines.append(f"Required Per Run: {required_per_run}")

    # Skills
    lines.append(f"Skills: {', '.join(skills_sorted)}")

    # Time
    lines.append(f"Time: {run_time_min}")

    # Probabilities table
    lines.append("Probabilities:")

    if tech_level == 'Tech II':
        # Tech II probability table
        lines.append("| Decryptor                       | Probability | Runs | ME | TE |")
        lines.append("|---------------------------------|-------------|------|----|----|")

        for dec in DECRYPTORS:
            base_prob = inv_data['probability']
            prob = calculate_invention_probability(base_prob, skill_level, dec['prob_mult'])
            prob_pct = round(prob * 100, 2)

            # Base ME=2, TE=4, base runs from SDE
            base_runs = 0  # For Tech II, base runs come from the blueprint itself
            # The SDE has the product quantity
            # Use the product info for base runs
            for (bp_t_id, act_id), products in sde.products.items():
                if act_id == 8:  # Invention
                    for p in products:
                        if p['productTypeID'] == display_id:
                            base_runs = p['quantity']
                            break

            # Apply decryptor modifiers
            final_runs = base_runs + dec['runs']
            final_me = 2 + dec['me']
            final_te = 4 + dec['te']

            dec_name = dec['name']
            lines.append(f"| {dec_name:<31} | {prob_pct:>10.2f} | {final_runs:<4} | {final_me:<2} | {final_te:<2} |")
    else:
        # Tech III - need to determine relic from blueprint or materials
        lines.append("| Invented From                | Decryptor                       | Probability | Runs | ME | TE |")
        lines.append("|------------------------------|---------------------------------|-------------|------|----|----|")

        for relic_name in TECH3_RELIC_PATTERNS:
            relic_baseline = TECH3_RELIC_BASELINES[relic_name]

            for dec in DECRYPTORS:
                base_prob = relic_baseline['base_prob']
                prob = calculate_invention_probability(base_prob, skill_level, dec['prob_mult'])
                prob_pct = round(prob * 100, 2)

                # Base ME=2, TE=4, base runs from table
                base_runs = relic_baseline['base_runs']
                final_runs = base_runs + dec['runs']
                final_me = 2 + dec['me']
                final_te = 4 + dec['te']

                dec_name = dec['name']
                lines.append(f"| {relic_name:<14} | {dec_name:<31} | {prob_pct:>10.2f} | {final_runs:<4} | {final_me:<2} | {final_te:<2} |")

    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(
        description='EVE Online Industry Recipe Tool',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  python industry.py recipe Naga --sde /path/to/sde
  python industry.py recipe "Barrage L Blueprint" --sde /path/to/sde
  python industry.py recipe Fernite Carbide --sde /path/to/sde
  python industry.py invention Barrage L --sde /path/to/sde
  python industry.py invention "Barrage L" --sde /path/to/sde --skill 5
'''
    )

    subparsers = parser.add_subparsers(dest='command', help='Command to run')

    recipe_parser = subparsers.add_parser('recipe', help='Show recipe for a product or blueprint')
    recipe_parser.add_argument('name', help='Product or blueprint name (exact, case-sensitive)')
    recipe_parser.add_argument('--sde', required=True, help='Path to SDE directory')

    invention_parser = subparsers.add_parser('invention', help='Show invention planning for a Tech II/Tech III product')
    invention_parser.add_argument('name', help='Product or blueprint name (exact, case-sensitive)')
    invention_parser.add_argument('--sde', required=True, help='Path to SDE directory')
    invention_parser.add_argument('--skill', type=int, choices=range(1, 6), default=5,
                                  help='Skill level for all relevant skills (1-5, default 5)')

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if not os.path.isdir(args.sde):
        print(f"Error: SDE directory '{args.sde}' does not exist.", file=sys.stderr)
        sys.exit(1)

    # Parse SDE
    sde = SDEParser(args.sde)

    if args.command == 'recipe':
        result = format_recipe(sde, args.name)
    elif args.command == 'invention':
        result = format_invention(sde, args.name, args.skill)
    else:
        parser.print_help()
        sys.exit(1)

    print(result)


if __name__ == '__main__':
    main()
