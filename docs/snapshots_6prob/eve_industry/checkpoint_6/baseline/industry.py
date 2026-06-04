#!/usr/bin/env python3
"""
EVE Online Industry Recipe Planner
Parses SDE data to generate deterministic recipe reports for products or blueprints.
"""

import argparse
import bz2
import csv
import math
import sys
import yaml
from collections import defaultdict
from pathlib import Path


class SDEParser:
    """Parser for EVE Online Static Data Export files."""

    def __init__(self, sde_dir: str):
        self.sde_dir = Path(sde_dir)
        self.item_by_id = {}
        self.item_by_name = {}
        self.group_by_id = {}
        self.category_by_id = {}
        self.market_group_by_id = {}
        self.market_group_parent = {}
        self.ship_volumes = {}

    def load_all(self):
        """Load all SDE data files."""
        self._load_types()
        self._load_groups()
        self._load_categories()
        self._load_market_groups()
        self._load_ship_volumes()

    def _load_csv(self, filename: str, fieldnames: list):
        """Load a CSV file with BZ2 compression."""
        filepath = self.sde_dir / filename
        records = []
        with bz2.open(filepath, 'rt', encoding='utf-8') as f:
            reader = csv.DictReader(f, fieldnames=fieldnames, delimiter=',')
            for row in reader:
                records.append(row)
        return records

    def _load_types(self):
        """Load invTypes.csv."""
        filepath = self.sde_dir / 'invTypes.csv.bz2'
        with bz2.open(filepath, 'rt', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row['published'] == '1':
                    type_id = int(row['typeID'])
                    self.item_by_id[type_id] = {
                        'typeID': type_id,
                        'groupID': int(row['groupID']),
                        'typeName': row['typeName'],
                        'volume': float(row['volume']) if row['volume'] and row['volume'] != '0E-10' else 0.0,
                        'marketGroupID': int(row['marketGroupID']) if row['marketGroupID'] and row['marketGroupID'] != 'None' else None
                    }
                    self.item_by_name[row['typeName']] = self.item_by_id[type_id]

    def _load_groups(self):
        """Load invGroups.csv."""
        filepath = self.sde_dir / 'invGroups.csv.bz2'
        with bz2.open(filepath, 'rt', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row['published'] == '1':
                    group_id = int(row['groupID'])
                    self.group_by_id[group_id] = {
                        'groupID': group_id,
                        'categoryID': int(row['categoryID']),
                        'groupName': row['groupName']
                    }

    def _load_categories(self):
        """Load invCategories.csv."""
        filepath = self.sde_dir / 'invCategories.csv.bz2'
        with bz2.open(filepath, 'rt', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row['published'] == '1':
                    category_id = int(row['categoryID'])
                    self.category_by_id[category_id] = {
                        'categoryID': category_id,
                        'categoryName': row['categoryName']
                    }

    def _load_market_groups(self):
        """Load invMarketGroups.csv."""
        filepath = self.sde_dir / 'invMarketGroups.csv.bz2'
        with bz2.open(filepath, 'rt', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                market_group_id = int(row['marketGroupID'])
                parent_id = row['parentGroupID']
                self.market_group_by_id[market_group_id] = {
                    'marketGroupID': market_group_id,
                    'marketGroupName': row['marketGroupName'],
                    'parentMarketGroupID': int(parent_id) if parent_id and parent_id != 'None' else None
                }

    def _load_ship_volumes(self):
        """Load ship_volumes.yaml."""
        filepath = self.sde_dir / 'ship_volumes.yaml'
        with open(filepath, 'r', encoding='utf-8') as f:
            self.ship_volumes = yaml.safe_load(f)


class IndustryPlanner:
    """Main industry planning logic."""

    ACTIVITY_MANUFACTURING = 1
    ACTIVITY_REACTIONS = 11
    ACTIVITY_INVENTION = 8
    ACTIVITY_REVERSE_ENGINEERING = 7

    # Decryptor modifiers for Tech II and Tech III invention
    DECRYPTOR_MODIFIERS = {
        'None': {'probability_mult': 0, 'runs': 0, 'me': 0, 'te': 0},
        'Accelerant Decryptor': {'probability_mult': 20, 'runs': 1, 'me': 2, 'te': 10},
        'Attainment Decryptor': {'probability_mult': 80, 'runs': 4, 'me': -1, 'te': 4},
        'Augmentation Decryptor': {'probability_mult': -40, 'runs': 9, 'me': -2, 'te': 2},
        'Optimized Attainment Decryptor': {'probability_mult': 90, 'runs': 2, 'me': 1, 'te': -2},
        'Optimized Augmentation Decryptor': {'probability_mult': -10, 'runs': 7, 'me': 2, 'te': 0},
        'Parity Decryptor': {'probability_mult': 50, 'runs': 3, 'me': 1, 'te': -2},
        'Process Decryptor': {'probability_mult': 10, 'runs': 0, 'me': 3, 'te': 6},
        'Symmetry Decryptor': {'probability_mult': 0, 'runs': 2, 'me': 1, 'te': 8},
    }

    # Tech III relic baselines
    TECH3_RELIC_BASELINES = {
        'Intact': {'base_chance': 0.26, 'base_runs': 20},
        'Malfunctioning': {'base_chance': 0.21, 'base_runs': 10},
        'Wrecked': {'base_chance': 0.14, 'base_runs': 3},
    }

    # Tech III relic type IDs by category
    TECH3_RELIC_TYPES = {
        'Intact Hull Section': 30752,
        'Malfunctioning Hull Section': 30753,
        'Wrecked Hull Section': 30754,
        'Small Intact Hull Section': 34412,
        'Small Malfunctioning Hull Section': 34414,
        'Small Wrecked Hull Section': 34416,
    }

    def __init__(self, sde_parser: SDEParser):
        self.parser = sde_parser
        self.industry_activities = self._load_industry_activities()
        self.industry_products = self._load_industry_products()
        self.industry_materials = self._load_industry_materials()
        self.industry_probabilities = self._load_industry_probabilities()
        self.industry_skills = self._load_industry_skills()

    def _load_industry_activities(self):
        """Load industryActivity.csv."""
        filepath = self.parser.sde_dir / 'industryActivity.csv.bz2'
        activities = {}
        with bz2.open(filepath, 'rt', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                key = (int(row['typeID']), int(row['activityID']))
                activities[key] = {
                    'time': int(row['time'])
                }
        return activities

    def _load_industry_products(self):
        """Load industryActivityProducts.csv."""
        filepath = self.parser.sde_dir / 'industryActivityProducts.csv.bz2'
        products = defaultdict(list)
        with bz2.open(filepath, 'rt', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                key = (int(row['typeID']), int(row['activityID']))
                products[key].append({
                    'productTypeID': int(row['productTypeID']),
                    'quantity': int(row['quantity'])
                })
        return products

    def _load_industry_materials(self):
        """Load industryActivityMaterials.csv."""
        filepath = self.parser.sde_dir / 'industryActivityMaterials.csv.bz2'
        materials = defaultdict(list)
        with bz2.open(filepath, 'rt', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                key = (int(row['typeID']), int(row['activityID']))
                materials[key].append({
                    'materialTypeID': int(row['materialTypeID']),
                    'quantity': int(row['quantity'])
                })
        return materials

    def _load_industry_probabilities(self):
        """Load industryActivityProbabilities.csv."""
        filepath = self.parser.sde_dir / 'industryActivityProbabilities.csv.bz2'
        probabilities = defaultdict(list)
        with bz2.open(filepath, 'rt', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                key = (int(row['typeID']), int(row['activityID']))
                probabilities[key].append({
                    'productTypeID': int(row['productTypeID']),
                    'probability': float(row['probability'])
                })
        return probabilities

    def _load_industry_skills(self):
        """Load industryActivitySkills.csv."""
        filepath = self.parser.sde_dir / 'industryActivitySkills.csv.bz2'
        skills = defaultdict(list)
        with bz2.open(filepath, 'rt', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                key = (int(row['typeID']), int(row['activityID']))
                skills[key].append({
                    'skillID': int(row['skillID']),
                    'level': int(row['level'])
                })
        return skills

    def find_product(self, name: str) -> dict:
        """Find a product by name. Returns item dict or None."""
        if name in self.parser.item_by_name:
            return self.parser.item_by_name[name]
        return None

    def find_blueprint(self, name: str) -> dict:
        """Find a blueprint by name. Returns item dict or None."""
        # Look for "X Blueprint" pattern
        if name.endswith(' Blueprint'):
            product_name = name[:-11]
            # Find the blueprint for this product
            for type_name, item in self.parser.item_by_name.items():
                if type_name == name:
                    return item
        return None

    def get_item_name(self, type_id: int) -> str:
        """Get item name by type ID."""
        if type_id in self.parser.item_by_id:
            return self.parser.item_by_id[type_id]['typeName']
        return None

    def get_group_hierarchy(self, group_id: int) -> list:
        """Get the group hierarchy from category to group."""
        if group_id not in self.parser.group_by_id:
            return []
        group = self.parser.group_by_id[group_id]
        category_id = group['categoryID']
        category_name = self.parser.category_by_id.get(category_id, {}).get('categoryName', 'Unknown')
        return [category_name, group['groupName']]

    def get_market_group_path(self, market_group_id: int) -> list:
        """Get the market group hierarchy path."""
        path = []
        current_id = market_group_id
        while current_id and current_id in self.parser.market_group_by_id:
            mg = self.parser.market_group_by_id[current_id]
            path.insert(0, mg['marketGroupName'])
            current_id = mg['parentMarketGroupID']
        return path

    def get_packaged_volume(self, item: dict) -> float:
        """Get packaged volume. For ships, use ship_volumes.yaml."""
        type_id = item['typeID']
        group_id = item['groupID']

        if group_id in self.parser.group_by_id:
            group = self.parser.group_by_id[group_id]
            group_name = group['groupName']
            category_id = group['categoryID']
            category = self.parser.category_by_id.get(category_id, {})

            # Check if it's a ship
            if category.get('categoryName') == 'Ship':
                if group_name in self.parser.ship_volumes:
                    return self.parser.ship_volumes[group_name]

        return item['volume']

    def get_tech_level(self, item: dict) -> str:
        """Determine tech level of an item."""
        type_id = item['typeID']
        group_id = item['groupID']

        # Check meta types for tech levels
        filepath = self.parser.sde_dir / 'invMetaTypes.csv.bz2'
        with bz2.open(filepath, 'rt', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if int(row['typeID']) == type_id:
                    meta_group_id = int(row['metaGroupID']) if row['metaGroupID'] else 0
                    # Meta groups: 1 = Tech I, 2 = Tech II, 3 = Tech III
                    if meta_group_id == 2:
                        return 'Tech II'
                    elif meta_group_id == 3:
                        return 'Tech III'

        # Check for blueprint pattern
        name = item['typeName']
        if 'Blueprint' in name:
            # Blueprint of certain types indicate tech level
            pass

        # Default to Tech I
        return 'Tech I'

    def get_skill_name(self, skill_id: int) -> str:
        """Get skill name by skill ID."""
        if skill_id in self.parser.item_by_id:
            return self.parser.item_by_id[skill_id]['typeName']
        return f'Unknown Skill ({skill_id})'

    def find_bp_for_product(self, product_type_id: int) -> dict:
        """Find the blueprint that manufactures a given product."""
        for (bp_id, act_id), products in self.industry_products.items():
            if act_id == self.ACTIVITY_MANUFACTURING:
                for p in products:
                    if p['productTypeID'] == product_type_id:
                        return self.parser.item_by_id.get(bp_id)
        return None

    def find_invention_parent_for_t2_blueprint(self, t2_blueprint_type_id: int) -> dict:
        """
        Find the T1 blueprint that invents a Tech II blueprint.
        The T2 blueprint (e.g., Barrage L Blueprint) is created via Invention on the T1 blueprint.
        """
        for (parent_id, act_id), products in self.industry_products.items():
            if act_id == self.ACTIVITY_INVENTION:
                for p in products:
                    if p['productTypeID'] == t2_blueprint_type_id:
                        return self.parser.item_by_id.get(parent_id)
        return None

    def get_invention_data(self, product_name: str, skill_level: int = 5) -> dict:
        """
        Get invention data for a product.

        For Tech II: Invention from Tech I blueprint (activityID=8)
        For Tech III: Reverse Engineering (activityID=7)
        """
        # Find the Tech II product
        product = self.find_product(product_name)
        is_tech3 = False

        if not product:
            return None

        type_id = product['typeID']
        name = product['typeName']
        tech_level = self.get_tech_level(product)

        # Determine if Tech II or Tech III
        if tech_level == 'Tech II':
            activity_id = self.ACTIVITY_INVENTION
            is_tech3 = False
        elif tech_level == 'Tech III':
            activity_id = self.ACTIVITY_REVERSE_ENGINEERING
            is_tech3 = True
        else:
            return None

        if is_tech3:
            # Tech III: use product's data
            activity_type_id = type_id
            t2_bp = self.find_bp_for_product(type_id)
            t1_bp = None
            parent_bp = None
            item_type_id = type_id
        else:
            # Tech II: find the blueprint that manufactures this product
            t2_bp = self.find_bp_for_product(type_id)
            if not t2_bp:
                # The product itself might be a blueprint
                t2_bp = product

            t2_bp_id = t2_bp['typeID']
            # Find the T1 blueprint that invents the T2 blueprint
            t1_bp = self.find_invention_parent_for_t2_blueprint(t2_bp_id)
            if not t1_bp:
                return None

            parent_bp = t1_bp  # This is what appears in the Base line
            item_type_id = t2_bp_id  # The blueprint's type ID is used for materials/skills
            activity_type_id = t1_bp['typeID']  # Activity is on the T1 blueprint

        # Get activity data
        activity_key = (activity_type_id, activity_id)
        activity_data = self.industry_activities.get(activity_key, {})
        run_time_seconds = activity_data.get('time', 0)
        run_time_minutes = math.ceil(run_time_seconds / 60)

        # Get materials (datacores) - from the T2 blueprint's perspective
        materials_list = self.industry_materials.get((item_type_id, activity_id), [])

        # If no materials on T2 blueprint, fallback to T1 blueprint
        if not materials_list:
            materials_list = self.industry_materials.get((activity_type_id, activity_id), [])

        # Get skills - from the T2 blueprint's perspective
        skills_list = self.industry_skills.get((item_type_id, activity_id), [])

        # If no skills on T2 blueprint, fallback to T1 blueprint
        if not skills_list:
            skills_list = self.industry_skills.get((activity_type_id, activity_id), [])

        # Get unique datacores per run
        datacore_type_ids = set()
        for mat in materials_list:
            datacore_type_ids.add(mat['materialTypeID'])

        if is_tech3:
            # Tech III: relic baselines
            relic_types = self._get_relic_type_for_product(product)
            if not relic_types:
                relic_types = [
                    (30752, 'Intact'),
                    (30753, 'Malfunctioning'),
                    (30754, 'Wrecked')
                ]
            base_probability = None
            base_runs = None
        else:
            # Tech II: get probability from T1 blueprint's invention activity
            prob_data = self.industry_probabilities.get((t1_bp['typeID'], activity_id), [])
            base_probability = 0.0
            base_runs = 0
            if prob_data:
                base_probability = prob_data[0]['probability']
                # Get base runs from industryActivityProducts
                products_data = self.industry_products.get((t1_bp['typeID'], activity_id), [])
                if products_data:
                    base_runs = products_data[0]['quantity']

        # For skills, use the --skill level for all relevant skills
        # The skills list from SDE gives us the skill IDs, but we replace levels
        processed_skills = []
        for skill in skills_list:
            processed_skills.append({
                'skillID': skill['skillID'],
                'level': skill_level  # Use --skill for all relevant skills
            })

        # Identify science and encryption skills
        encryption_skill_id = None
        science_skill_ids = []
        for skill in skills_list:
            skill_name = self.get_skill_name(skill['skillID'])
            if 'Encryption Methods' in skill_name:
                encryption_skill_id = skill['skillID']
            else:
                science_skill_ids.append(skill['skillID'])

        # Store counts of science and encryption skills for probability calculation
        # The --skill value applies to each skill's level
        num_science = len(science_skill_ids)
        has_encryption = 1 if encryption_skill_id else 0

        return {
            'item': product,
            'type_id': type_id,
            'name': name,
            'tech_level': tech_level,
            'is_tech3': is_tech3,
            'activity_id': activity_id,
            'activity_type_id': activity_type_id,
            't2_bp': t2_bp,
            't1_bp': t1_bp,
            'parent_bp': parent_bp,
            'run_time_minutes': run_time_minutes,
            'materials': materials_list,
            'skills': processed_skills,
            'datacore_type_ids': list(datacore_type_ids),
            'skill_level': skill_level,
            'base_probability': base_probability,
            'base_runs': base_runs,
            'encryption_skill_id': encryption_skill_id,
            'science_skill_ids': science_skill_ids,
            'num_science_skills': num_science,
            'has_encryption_skill': has_encryption,
        }

    def is_buildable(self, material_type_id: int) -> bool:
        """Check if a material can be produced via industry."""
        # Check if the material has any industry activity (manufacturing or reactions)
        for (type_id, activity_id), materials in self.industry_materials.items():
            if type_id == material_type_id and activity_id in (self.ACTIVITY_MANUFACTURING, self.ACTIVITY_REACTIONS):
                return True
            if type_id == material_type_id and activity_id in (3, 4):  # Research activities
                return True

        # Also check if it has product entries (is a product of something)
        for (type_id, activity_id), products in self.industry_products.items():
            for p in products:
                if p['productTypeID'] == material_type_id:
                    return True

        return False

    def get_recipe_for_product(self, product_name: str, material_efficiency: int = 0,
                                  time_efficiency: int = 0, display_waste: bool = False) -> dict:
        """Get recipe for a product with optional ME/TE adjustments."""
        # First, try to find the product directly
        product = self.find_product(product_name)
        if not product:
            # Try to find blueprint instead
            blueprint = self.find_blueprint(product_name)
            if blueprint:
                product = blueprint

        if not product:
            return None

        type_id = product['typeID']
        name = product['typeName']

        # Check if this is a blueprint
        is_blueprint = 'Blueprint' in name

        # Determine the actual product ID we're producing
        if is_blueprint:
            # For blueprints, we need to find what they produce
            product_type_id = None
            for (bp_id, activity_id), products in self.industry_products.items():
                if bp_id == type_id and activity_id == self.ACTIVITY_MANUFACTURING:
                    for p in products:
                        product_type_id = p['productTypeID']
                        break
                if product_type_id:
                    break

            if not product_type_id:
                # This blueprint might be for reactions or something else
                # Try to find any product
                for (bp_id, activity_id), products in self.industry_products.items():
                    if bp_id == type_id:
                        product_type_id = products[0]['productTypeID']
                        break

            if not product_type_id:
                return None

            blueprint_type_id = type_id
            activity_type_id = self.ACTIVITY_MANUFACTURING
            activity_key = (type_id, activity_type_id)

        else:
            # This is a product, find its blueprint
            product_type_id = type_id
            blueprint_type_id = None
            activity_type_id = None

            # Find the blueprint that produces this
            for (bp_id, activity_id), products in self.industry_products.items():
                for p in products:
                    if p['productTypeID'] == type_id:
                        blueprint_type_id = bp_id
                        activity_type_id = activity_id
                        product_quantity = p['quantity']
                        break
                if blueprint_type_id:
                    break

            if not blueprint_type_id:
                # Maybe this is a reaction product? Check if it's produced via reactions
                # Check industryActivity for reactions
                for (mat_type_id, activity_id) in self.industry_activities:
                    if activity_id == self.ACTIVITY_REACTIONS:
                        # Check if this material is produced
                        mat_key = (mat_type_id, activity_id)
                        if mat_key in self.industry_products:
                            for p in self.industry_products[mat_key]:
                                if p['productTypeID'] == type_id:
                                    blueprint_type_id = mat_type_id
                                    activity_type_id = activity_id
                                    product_quantity = p['quantity']
                                    break
                        if blueprint_type_id:
                            break

            if not blueprint_type_id:
                return None

            activity_key = (blueprint_type_id, activity_type_id)

        # Get activity time
        activity_data = self.industry_activities.get(activity_key, {})
        run_time_seconds = activity_data.get('time', 0)
        run_time_minutes = math.ceil(run_time_seconds / 60)

        # Get materials
        materials_list = self.industry_materials.get(activity_key, [])

        # Get output quantity
        output_quantity = 1
        if activity_key in self.industry_products:
            products = self.industry_products[activity_key]
            for p in products:
                if is_blueprint or p['productTypeID'] == product_type_id:
                    output_quantity = p['quantity']
                    break

        # Get activity name
        activity_name = 'Manufacturing' if activity_type_id == self.ACTIVITY_MANUFACTURING else 'Reactions'

        # Apply ME and TE adjustments
        adjusted_run_time = run_time_minutes
        adjusted_materials = []
        max_me = 10  # Max ME is 10

        if activity_type_id == self.ACTIVITY_MANUFACTURING:
            # Apply Time Efficiency: reduces run time by TE/100 (but not below 0)
            if time_efficiency > 0:
                # TE of 10 means 10% faster, TE of 20 means 20% faster
                time_reduction = time_efficiency / 100
                adjusted_run_time = max(0, int(math.ceil(run_time_minutes * (1 - time_reduction))))

            # Apply Material Efficiency for Manufacturing
            if material_efficiency > 0 and activity_type_id == self.ACTIVITY_MANUFACTURING:
                for mat in materials_list:
                    mat_quantity = mat['quantity']
                    # ME reduces material needed: quantity * (1 - ME/100)
                    # Use max(1, ...) to ensure at least 1
                    adjusted_qty = math.ceil(max(1, mat_quantity * (1 - material_efficiency / 100)))
                    waste = mat_quantity - adjusted_qty if display_waste else 0
                    adjusted_materials.append({
                        **mat,
                        'quantity': adjusted_qty,
                        'waste': waste,
                        'original_quantity': mat_quantity
                    })
            else:
                # No ME, but still pass through materials with waste info for --display-waste
                for mat in materials_list:
                    adjusted_materials.append({
                        **mat,
                        'waste': 0,
                        'original_quantity': mat['quantity']
                    })
        else:
            # Reactions are not affected by ME
            adjusted_materials = [{**mat, 'waste': 0, 'original_quantity': mat['quantity']} for mat in materials_list]
            if time_efficiency > 0:
                time_reduction = time_efficiency / 100
                adjusted_run_time = max(0, int(math.ceil(run_time_minutes * (1 - time_reduction))))

        return {
            'item': product,
            'item_type_id': product_type_id if product_type_id else blueprint_type_id,
            'blueprint_type_id': blueprint_type_id,
            'activity': activity_name,
            'activity_id': activity_type_id,
            'output_quantity': output_quantity,
            'run_time': adjusted_run_time,
            'materials': adjusted_materials,
            'material_efficiency': material_efficiency,
            'time_efficiency': time_efficiency,
            'display_waste': display_waste
        }

    def format_recipe(self, recipe: dict) -> str:
        """Format the canonical recipe output."""
        item = recipe['item']
        type_id = item['typeID']
        name = item['typeName']

        # Get group hierarchy
        group_hierarchy = self.get_group_hierarchy(item['groupID'])
        group_str = ' > '.join(group_hierarchy) if group_hierarchy else 'Unknown'

        # Get market group path
        market_group_path = []
        if item['marketGroupID']:
            market_group_path = self.get_market_group_path(item['marketGroupID'])
        market_group_str = ' > '.join(market_group_path) if market_group_path else 'None'

        # Get tech level
        tech_level = self.get_tech_level(item)

        # Get volume
        volume = self.get_packaged_volume(item)

        # Get ME/TE info
        me = recipe.get('material_efficiency', 0)
        te = recipe.get('time_efficiency', 0)
        display_waste = recipe.get('display_waste', False)

        # Build material rows
        material_rows = []
        for mat in recipe['materials']:
            mat_type_id = mat['materialTypeID']
            mat_name = self.get_item_name(mat_type_id) or f'Unknown ({mat_type_id})'
            mat_quantity = mat['quantity']
            mat_buildable = 'Yes' if self.is_buildable(mat_type_id) else 'No'
            mat_waste = mat.get('waste', 0)
            if display_waste:
                material_rows.append((mat_name, mat_quantity, mat_waste, mat_buildable))
            else:
                material_rows.append((mat_name, mat_quantity, mat_buildable))

        # Sort alphabetically by item name (case-insensitive)
        material_rows.sort(key=lambda x: x[0].lower())

        run_time_str = f'Run Time: {recipe["run_time"]}'
        if te > 0:
            run_time_str += f' (TE: {te})'

        lines = [
            f'ITEM: {name} ({type_id})',
            f'Group: {group_str}',
            f'Market Group: {market_group_str}',
            f'Tech Level: {tech_level}',
            f'Volume: {volume:.2f}',
            '',
            'Recipe:',
            f'Activity: {recipe["activity"]}',
            f'Output Quantity: {recipe["output_quantity"]}',
            run_time_str,
        ]

        if display_waste:
            lines.append('| Item | Quantity | Waste | Buildable |')
            lines.append('|:-:|:---:|:---:|---:|')
            for mat_name, mat_quantity, mat_waste, mat_buildable in material_rows:
                lines.append(f'| {mat_name} | {mat_quantity} | {mat_waste} | {mat_buildable} |')
        else:
            lines.append('| Item | Quantity | Buildable |')
            lines.append('|:-:|:---:|---:|')
            for mat_name, mat_quantity, mat_buildable in material_rows:
                lines.append(f'| {mat_name} | {mat_quantity} | {mat_buildable} |')

        return '\n'.join(lines)

    def generate_plan_with_stocks_and_targets(self, build_string: str, config: dict,
                                               stocks: dict, targets: dict) -> dict:
        """
        Generate a build plan that also accounts for stock levels and targets.
        This combines the main build with stock replenishment.
        """
        # First, generate the base plan
        base_plan = self.generate_build_all_plan(build_string, config)

        # Get config values
        te_bonus = config.get('te_bonus', 0)
        split_days = config.get('split_days', 3.0)
        comp_defaults = config.get('component_bpo_defaults', {})
        comp_me = comp_defaults.get('me', 10)
        comp_te = comp_defaults.get('te', 20)
        invention_skill = config.get('invention_skill', 3)

        # Track all materials: name -> {'start': int, 'consumed': int, 'produced': int}
        materials = {}

        # Initialize from stocks
        for name, qty in stocks.items():
            materials[name] = {'start': qty, 'consumed': 0, 'produced': 0}

        # Process the base plan's materials
        for mat in base_plan['materials']:
            name = mat['name']
            consumed = mat['to_buy']

            if name not in materials:
                materials[name] = {'start': 0, 'consumed': 0, 'produced': 0}
            materials[name]['consumed'] += consumed

        # Track base plan jobs
        base_jobs = base_plan['jobs'][:]

        # Process stock targets
        for target_name, target_qty in targets.items():
            current = stocks.get(target_name, 0)
            deficit = target_qty - current

            if deficit <= 0:
                # Target already met, but track available stock
                if target_name not in materials:
                    materials[target_name] = {'start': current, 'consumed': 0, 'produced': 0}
                continue

            # Find product
            product = self.find_product(target_name)
            if not product:
                # Not found - must buy
                if target_name not in materials:
                    materials[target_name] = {'start': current, 'consumed': 0, 'produced': 0}
                materials[target_name]['consumed'] += deficit
                continue

            # Check if inventable (T2/T3)
            tech_level = self.get_tech_level(product)
            is_inventable = tech_level in ['Tech II', 'Tech III']
            invention_data = None

            if is_inventable:
                invention_data = self.get_invention_data(target_name, invention_skill)

            # Check if buildable
            bp = self.find_bp_for_product(product['typeID'])
            is_reaction = False

            if not bp:
                for (type_id, activity_id) in self.industry_activities:
                    if activity_id == self.ACTIVITY_REACTIONS:
                        if type_id == product['typeID']:
                            is_reaction = True
                            break

            is_buildable = bp or is_reaction

            if is_inventable and invention_data:
                # Inventable via invention
                base_runs = invention_data.get('base_runs', 10)
                runs_needed = (deficit + base_runs - 1) // base_runs
                produced = runs_needed * base_runs

                # Get invention materials
                mats = invention_data.get('materials', [])
                if not mats:
                    mats = self.industry_materials.get(
                        (invention_data.get('activity_type_id', 0), invention_data['activity_id']), [])

                for mat in mats:
                    mat_name = self.get_item_name(mat['materialTypeID']) or f'Unknown ({mat["materialTypeID"]})'
                    mat_qty = mat['quantity'] * runs_needed

                    if mat_name not in materials:
                        start = stocks.get(mat_name, 0)
                        materials[mat_name] = {'start': start, 'consumed': 0, 'produced': 0}
                    materials[mat_name]['consumed'] += mat_qty

                # Add job for invention
                job_name = invention_data['name'] + ' Blueprint' if 'Blueprint' not in invention_data['name'] else invention_data['name']
                base_jobs.append({
                    'name': 'Inventing ' + job_name,
                    'runs': runs_needed,
                    'days': math.ceil(invention_data.get('run_time_minutes', 0) * (1 - te_bonus / 100)) / 1440,
                    'count': 1
                })

            elif is_buildable:
                # Buildable via manufacturing or reactions
                if bp:
                    activity_key = (bp['typeID'], self.ACTIVITY_MANUFACTURING)
                    output_per_run = 1
                    if activity_key in self.industry_products:
                        products_list = self.industry_products[activity_key]
                        for p in products_list:
                            if p['productTypeID'] == product['typeID']:
                                output_per_run = p['quantity']
                                break

                    runs_needed = (deficit + output_per_run - 1) // output_per_run
                    produced = runs_needed * output_per_run

                    # Get manufacturing materials with ME applied
                    mats = self.industry_materials.get(activity_key, [])
                    for mat in mats:
                        mat_name = self.get_item_name(mat['materialTypeID']) or f'Unknown ({mat["materialTypeID"]})'
                        orig_qty = mat['quantity']
                        adjusted = math.ceil(max(1, orig_qty * (1 - comp_me / 100)))
                        total_qty = adjusted * runs_needed

                        if mat_name not in materials:
                            start = stocks.get(mat_name, 0)
                            materials[mat_name] = {'start': start, 'consumed': 0, 'produced': 0}
                        materials[mat_name]['consumed'] += total_qty

                    job_name = bp['typeName']
                    base_jobs.append({
                        'name': job_name,
                        'runs': runs_needed,
                        'days': math.ceil(
                            self.industry_activities.get(activity_key, {}).get('time', 0) / 60 *
                            (1 - comp_te / 100) * (1 - te_bonus / 100)
                        ) / 1440,
                        'count': 1
                    })
                else:
                    # Reaction
                    activity_key = (product['typeID'], self.ACTIVITY_REACTIONS)
                    output_per_run = 1
                    if activity_key in self.industry_products:
                        products_list = self.industry_products[activity_key]
                        for p in products_list:
                            output_per_run = p['quantity']
                            break

                    runs_needed = (deficit + output_per_run - 1) // output_per_run
                    produced = runs_needed * output_per_run

                    # Get reaction materials (no ME)
                    mats = self.industry_materials.get(activity_key, [])
                    for mat in mats:
                        mat_name = self.get_item_name(mat['materialTypeID']) or f'Unknown ({mat["materialTypeID"]})'
                        mat_qty = mat['quantity'] * runs_needed

                        if mat_name not in materials:
                            start = stocks.get(mat_name, 0)
                            materials[mat_name] = {'start': start, 'consumed': 0, 'produced': 0}
                        materials[mat_name]['consumed'] += mat_qty

                    job_name = product['typeName'] + ' Reaction Formula'
                    base_jobs.append({
                        'name': job_name,
                        'runs': runs_needed,
                        'days': math.ceil(
                            self.industry_activities.get(activity_key, {}).get('time', 0) / 60 *
                            (1 - te_bonus / 100)
                        ) / 1440,
                        'count': 1
                    })

                # Track production
                if job_name not in materials:
                    materials[job_name] = {'start': 0, 'consumed': 0, 'produced': produced}
                else:
                    materials[job_name]['produced'] += produced
            else:
                # Not buildable, must buy
                if target_name not in materials:
                    materials[target_name] = {'start': current, 'consumed': 0, 'produced': 0}
                materials[target_name]['consumed'] += deficit

        # Build final materials list
        final_materials = []
        for name in sorted(materials.keys(), key=str.lower):
            data = materials[name]
            end = data['start'] - data['consumed'] + data['produced']
            final_materials.append({
                'name': name,
                'to_buy': max(0, data['consumed']),
                'buy_volume': math.ceil(max(0, data['consumed']) / 100) * 100,
                'start_amount': data['start'],
                'end_amount': max(0, end)
            })

        # Sort jobs
        base_jobs.sort(key=lambda x: x['name'].lower())

        return {
            'materials': final_materials,
            'jobs': base_jobs
        }

    def load_config(self, config_path: str) -> dict:
        """Load YAML configuration file."""
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        return config or {}

    def load_stock_file(self, filepath: str) -> dict:
        """Load a stock or target file.
        Format: {Item Name} {Quantity} with lenient parsing for extra text.
        Returns dict mapping item name to quantity."""
        stocks = {}
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                # Split on whitespace, take first two parts
                parts = line.split()
                if len(parts) >= 2:
                    # Find the item name (everything except the last numeric part)
                    item_name_parts = []
                    quantity = None

                    for i, part in enumerate(parts):
                        # Check if this part is a number (quantity)
                        if part.isdigit():
                            quantity = int(part)
                            # Everything before this is the item name
                            item_name_parts = parts[:i]
                            break

                    if item_name_parts and quantity is not None:
                        item_name = ' '.join(item_name_parts)
                        stocks[item_name] = quantity

        return stocks

    def calculate_stock_targets(self, stocks: dict, targets: dict, comp_me: int, comp_te: int,
                                invention_skill: int, te_bonus: int) -> dict:
        """
        Calculate what needs to be built/bought to meet target stock levels.
        Returns a dict with:
        - 'to_build': dict of items to build and quantity needed
        - 'to_buy': dict of items to buy and quantity needed
        - 'materials': dict of materials needed for inventable/buildable items
        """
        to_build = {}
        to_buy = {}
        materials = {}

        for target_name, target_qty in targets.items():
            current_qty = stocks.get(target_name, 0)
            deficit = target_qty - current_qty

            if deficit <= 0:
                # Target already met or exceeded
                continue

            # Check if this item is buildable (can be manufactured or reacted)
            product = self.find_product(target_name)
            is_inventable = False
            invention_data = None
            is_buildable = False

            if product:
                tech_level = self.get_tech_level(product)
                if tech_level in ['Tech II', 'Tech III']:
                    is_inventable = True
                    invention_data = self.get_invention_data(target_name, invention_skill)

            # Check if buildable via manufacturing or reactions
            if product:
                bp = self.find_bp_for_product(product['typeID'])
                if bp:
                    is_buildable = True
                else:
                    # Check if it's produced via reactions
                    for (type_id, activity_id) in self.industry_activities:
                        if activity_id == self.ACTIVITY_REACTIONS:
                            if type_id == product['typeID']:
                                is_buildable = True
                                break

            if is_inventable and invention_data:
                # Inventable item (T2/T3) - calculate using invention runs
                # Base runs from invention (without decryptor, per spec)
                base_runs = invention_data.get('base_runs', 10)

                runs_needed = (deficit + base_runs - 1) // base_runs

                # Get materials for invention
                materials_list = invention_data.get('materials', [])
                if not materials_list:
                    materials_list = self.industry_materials.get(
                        (invention_data.get('activity_type_id', 0), invention_data['activity_id']), [])

                for mat in materials_list:
                    mat_type_id = mat['materialTypeID']
                    mat_qty = mat['quantity'] * runs_needed
                    mat_name = self.get_item_name(mat_type_id) or f'Unknown ({mat_type_id})'
                    materials[mat_name] = materials.get(mat_name, 0) + mat_qty

                job_name = invention_data['name'] + ' Blueprint' if 'Blueprint' not in invention_data['name'] else invention_data['name']
                to_build[job_name] = to_build.get(job_name, 0) + runs_needed

            elif is_buildable:
                # Buildable item via manufacturing/reactions
                # Calculate using component ME/TE from config
                # Find the product or blueprint and get recipe
                if product:
                    product_type_id = product['typeID']
                else:
                    # Try to find blueprint directly
                    bp = self.find_product(target_name + ' Blueprint')
                    if bp:
                        product_type_id = bp['typeID']
                    else:
                        # Not findable, must buy
                        to_buy[target_name] = to_buy.get(target_name, 0) + deficit
                        continue

                # Get blueprint and materials
                bp = self.find_bp_for_product(product_type_id)
                if bp:
                    activity_key = (bp['typeID'], self.ACTIVITY_MANUFACTURING)
                    output_per_run = 1
                    if activity_key in self.industry_products:
                        products = self.industry_products[activity_key]
                        for p in products:
                            output_per_run = p['quantity']
                            break

                    runs_needed = (deficit + output_per_run - 1) // output_per_run

                    # Get materials with ME/TE applied
                    materials_list = self.industry_materials.get(activity_key, [])
                    for mat in materials_list:
                        mat_type_id = mat['materialTypeID']
                        original_qty = mat['quantity']
                        adjusted_qty = math.ceil(max(1, original_qty * (1 - comp_me / 100)))
                        total_mat_qty = adjusted_qty * runs_needed
                        mat_name = self.get_item_name(mat_type_id) or f'Unknown ({mat_type_id})'
                        materials[mat_name] = materials.get(mat_name, 0) + total_mat_qty

                    job_name = bp['typeName']
                    to_build[job_name] = to_build.get(job_name, 0) + runs_needed
                else:
                    # Check if it's a reaction product
                    for (type_id, activity_id) in self.industry_activities:
                        if activity_id == self.ACTIVITY_REACTIONS:
                            if type_id == product_type_id:
                                activity_key = (type_id, activity_id)
                                output_per_run = 1
                                if activity_key in self.industry_products:
                                    products = self.industry_products[activity_key]
                                    for p in products:
                                        output_per_run = p['quantity']
                                        break

                                runs_needed = (deficit + output_per_run - 1) // output_per_run

                                # Get materials (no ME for reactions)
                                materials_list = self.industry_materials.get(activity_key, [])
                                for mat in materials_list:
                                    mat_type_id = mat['materialTypeID']
                                    mat_qty = mat['quantity'] * runs_needed
                                    mat_name = self.get_item_name(mat_type_id) or f'Unknown ({mat_type_id})'
                                    materials[mat_name] = materials.get(mat_name, 0) + mat_qty

                                product_name = self.get_item_name(product_type_id) or f'Unknown ({product_type_id})'
                                if not product_name.endswith(' Reaction Formula'):
                                    product_name = product_name + ' Reaction Formula'
                                to_build[product_name] = to_build.get(product_name, 0) + runs_needed
                                break
                    else:
                        # Not findable, must buy
                        to_buy[target_name] = to_buy.get(target_name, 0) + deficit
                        continue
            else:
                # Not buildable, must buy
                to_buy[target_name] = to_buy.get(target_name, 0) + deficit

        return {
            'to_build': to_build,
            'to_buy': to_buy,
            'materials': materials
        }

    def parse_build_string(self, build_string: str) -> dict:
        """
        Parse build string into components.
        Supports 4 formats:
        1. Item format: {Item name} {Runs Per Job} x{Number of Jobs} (ME/TE assumed 0)
        2. Normal format: {Item name} {Runs Per Job} {ME} {TE} x{Number of Jobs}
        3. Invention format: {Item name} {RunsPerJob} {DecryptorName|None} x{NumJobs}
        4. Reverse Engineering format: {Item name} {RunsPerJob} {DecryptorName|None} {RelicName} x{NumJobs}
        """
        # Known decryptor names (partial matches for relic names)
        decryptor_names = list(self.DECRYPTOR_MODIFIERS.keys())
        relic_types = ['Intact', 'Malfunctioning', 'Wrecked']

        parts = build_string.split()
        result = {
            'jobs': 1,
            'runs_per_job': 1,
            'me': 0,
            'te': 0,
            'decryptor': 'None',
            'relic': None,
            'format': 'unknown'
        }

        if len(parts) < 2:
            raise ValueError(f"Invalid build string format: {build_string}")

        # Find the 'x' token if present (indicates number of jobs)
        # It could be in the format 'x10' (no space) or 'x 10' (with space)
        x_index = -1
        for i, part in enumerate(parts):
            if part.lower() == 'x':
                x_index = i
                break
            elif part.lower().startswith('x'):
                # Handle 'x10' format
                if len(part) == 1:
                    # Just 'x'
                    x_index = i
                    break
                else:
                    # 'x10' - extract the number
                    try:
                        result['jobs'] = int(part[1:])
                    except ValueError:
                        raise ValueError(f"Invalid job count: {part[1:]}")
                    item_part = parts[:i]
                    return self._finalize_parse(item_part, result, decryptor_names, relic_types)

        if x_index != -1:
            # x{Number of Jobs} is present (with space)
            if x_index + 1 >= len(parts):
                raise ValueError(f"Invalid build string format: missing job count after x")
            try:
                result['jobs'] = int(parts[x_index + 1])
            except ValueError:
                raise ValueError(f"Invalid job count: {parts[x_index + 1]}")
            item_part = parts[:x_index]
        else:
            item_part = parts

        return self._finalize_parse(item_part, result, decryptor_names, relic_types)

    def _finalize_parse(self, item_part, result, decryptor_names, relic_types):
        """Finalize parsing by analyzing item_part to determine format."""
        # Analyze item_part to determine format
        # Work backwards to identify the parameters

        # Try to find a numeric value that's likely runs_per_job
        # The runs_per_job should be followed by either ME/TE or decryptor/relic or be the last parameter

        # Iterate through possible splits to find where item name ends
        num_elements = len(item_part)

        # Try format detection from the end
        # Check if we have 1 parameter (runs) then x{jobs}
        # Check if we have 2 parameters (runs, then decryptor/relic) then x{jobs}
        # Check if we have 3 parameters (runs, ME, TE) then x{jobs}
        # Check if we have just item name (no runs, no x)

        # Try parsing from the end backward
        params = []
        item_name_parts = []

        # Start from the end and collect parameters until we hit something that can't be a parameter
        i = num_elements - 1

        while i >= 0:
            part = item_part[i]

            # Check if it's a number
            if part.isdigit():
                params.insert(0, part)
                i -= 1
            # Check if it's a known decryptor
            elif part in decryptor_names:
                params.insert(0, part)
                i -= 1
            # Check if it's a relic type indicator
            elif any(rt in part for rt in relic_types):
                params.insert(0, part)
                i -= 1
            else:
                # Not a parameter, so it's part of item name
                break

        # Everything left is item name
        item_name_parts = item_part[:i+1]

        if not item_name_parts:
            raise ValueError(f"Invalid build string format: could not parse item name")

        result['item_name'] = ' '.join(item_name_parts)

        # Now analyze the parameters
        if len(params) == 0:
            # No parameters: {Item} (assume runs=1, jobs=1, ME=0, TE=0)
            result['runs_per_job'] = 1
            result['me'] = 0
            result['te'] = 0
            result['jobs'] = 1
            result['format'] = 'item'

        elif len(params) == 1:
            # One parameter: {Item} {Runs} x{Jobs} (Item format)
            if params[0].isdigit():
                result['runs_per_job'] = int(params[0])
                result['me'] = 0
                result['te'] = 0
                result['format'] = 'item'
            else:
                raise ValueError(f"Invalid build string format: expected numeric runs, got {params[0]}")

        elif len(params) == 2:
            # Two parameters: could be {Item} {Runs} {Decryptor|Relic} x{Jobs}
            # OR {Item} {Runs} x{Jobs} with item name having 2 words (but that's handled above)
            if params[0].isdigit():
                result['runs_per_job'] = int(params[0])
                potential_name = params[1]

                # Check if it's a known relic
                is_relic = any(rt in potential_name for rt in relic_types)
                is_decryptor = potential_name in decryptor_names

                if is_relic:
                    result['relic'] = potential_name
                    result['format'] = 're_engineering'
                elif is_decryptor:
                    result['decryptor'] = potential_name
                    result['format'] = 'invention'
                else:
                    # Could be ME/TE? But ME/TE would require 2 numbers, not 1 number + non-number
                    raise ValueError(f"Invalid build string format: expected decryptor or relic, got {potential_name}")
            else:
                raise ValueError(f"Invalid build string format: expected numeric runs, got {params[0]}")

        elif len(params) == 3:
            # Three parameters: {Item} {Runs} {ME} {TE}
            if params[0].isdigit() and params[1].isdigit() and params[2].isdigit():
                result['runs_per_job'] = int(params[0])
                result['me'] = int(params[1])
                result['te'] = int(params[2])
                result['format'] = 'normal'
            else:
                raise ValueError(f"Invalid build string format: expected runs ME TE, got {params}")

        else:
            raise ValueError(f"Invalid build string format: too many parameters, got {params}")

        # Validate ME/TE ranges
        if not 0 <= result['me'] <= 10:
            raise ValueError(f"Material Efficiency must be in range [0,10], got {result['me']}")
        if not 0 <= result['te'] <= 20:
            raise ValueError(f"Time Efficiency must be in range [0,20], got {result['te']}")
        if result['te'] % 2 != 0:
            raise ValueError(f"Time Efficiency must be even, got {result['te']}")

        return result

        # The last element before x (if x exists) or the last element (if no x)
        # could be part of item name or a parameter

        # Strategy: Check if the last element looks like a parameter (numeric or known keyword)
        # If it is, then it's a parameter, not part of item name
        # If it's not, then it could be part of item name

        # Try different splits from the end
        # We'll try to identify the format by checking the ending elements

        # Try to find a numeric value that's likely runs_per_job
        # The runs_per_job should be followed by either ME/TE or decryptor/relic or be the last parameter

        # Iterate through possible splits to find where item name ends
        num_elements = len(item_part)

        # Try format detection from the end
        # Check if we have 1 parameter (runs) then x{jobs}
        # Check if we have 2 parameters (runs, then decryptor/relic) then x{jobs}
        # Check if we have 3 parameters (runs, ME, TE) then x{jobs}
        # Check if we have just item name (no runs, no x)

        # Try parsing from the end backward
        params = []
        item_name_parts = []

        # Start from the end and collect parameters until we hit something that can't be a parameter
        i = num_elements - 1

        while i >= 0:
            part = item_part[i]

            # Check if it's a number
            if part.isdigit():
                params.insert(0, part)
                i -= 1
            # Check if it's a known decryptor
            elif part in decryptor_names:
                params.insert(0, part)
                i -= 1
            # Check if it's a relic type indicator
            elif any(rt in part for rt in relic_types):
                params.insert(0, part)
                i -= 1
            else:
                # Not a parameter, so it's part of item name
                break

        # Everything left is item name
        item_name_parts = item_part[:i+1]

        if not item_name_parts:
            raise ValueError(f"Invalid build string format: could not parse item name from {build_string}")

        result['item_name'] = ' '.join(item_name_parts)

        # Now analyze the parameters
        if len(params) == 0:
            # No parameters: {Item} (assume runs=1, jobs=1, ME=0, TE=0)
            result['runs_per_job'] = 1
            result['me'] = 0
            result['te'] = 0
            result['jobs'] = 1
            result['format'] = 'item'

        elif len(params) == 1:
            # One parameter: {Item} {Runs} x{Jobs} (Item format)
            if params[0].isdigit():
                result['runs_per_job'] = int(params[0])
                result['me'] = 0
                result['te'] = 0
                result['format'] = 'item'
            else:
                raise ValueError(f"Invalid build string format: expected numeric runs, got {params[0]}")

        elif len(params) == 2:
            # Two parameters: could be {Item} {Runs} {Decryptor|Relic} x{Jobs}
            # OR {Item} {Runs} x{Jobs} with item name having 2 words (but that's handled above)
            if params[0].isdigit():
                result['runs_per_job'] = int(params[0])
                potential_name = params[1]

                # Check if it's a known relic
                is_relic = any(rt in potential_name for rt in relic_types)
                is_decryptor = potential_name in decryptor_names

                if is_relic:
                    result['relic'] = potential_name
                    result['format'] = 're_engineering'
                elif is_decryptor:
                    result['decryptor'] = potential_name
                    result['format'] = 'invention'
                else:
                    # Could be ME/TE? But ME/TE would require 2 numbers, not 1 number + non-number
                    raise ValueError(f"Invalid build string format: expected decryptor or relic, got {potential_name}")
            else:
                raise ValueError(f"Invalid build string format: expected numeric runs, got {params[0]}")

        elif len(params) == 3:
            # Three parameters: {Item} {Runs} {ME} {TE}
            if params[0].isdigit() and params[1].isdigit() and params[2].isdigit():
                result['runs_per_job'] = int(params[0])
                result['me'] = int(params[1])
                result['te'] = int(params[2])
                result['format'] = 'normal'
            else:
                raise ValueError(f"Invalid build string format: expected runs ME TE, got {params}")

        else:
            raise ValueError(f"Invalid build string format: too many parameters, got {params}")

        # Validate ME/TE ranges
        if not 0 <= result['me'] <= 10:
            raise ValueError(f"Material Efficiency must be in range [0,10], got {result['me']}")
        if not 0 <= result['te'] <= 20:
            raise ValueError(f"Time Efficiency must be in range [0,20], got {result['te']}")
        if result['te'] % 2 != 0:
            raise ValueError(f"Time Efficiency must be even, got {result['te']}")

        return result

    def format_invention_report(self, invention_data: dict) -> str:
        """Format the canonical invention report output."""
        item = invention_data['item']
        type_id = invention_data['type_id']
        name = invention_data['name']
        tech_level = invention_data['tech_level']
        is_tech3 = invention_data['is_tech3']

        # Get group hierarchy
        group_hierarchy = self.get_group_hierarchy(item['groupID'])
        group_str = ' > '.join(group_hierarchy) if group_hierarchy else 'Unknown'

        # Get market group path
        market_group_path = []
        if item['marketGroupID']:
            market_group_path = self.get_market_group_path(item['marketGroupID'])
        market_group_str = ' > '.join(market_group_path) if market_group_path else 'None'

        # Get datacores sorted alphabetically
        datacore_names = []
        for dt_id in invention_data['datacore_type_ids']:
            dt_name = self.get_item_name(dt_id)
            if dt_name:
                datacore_names.append(dt_name)
        datacore_names.sort(key=str.lower)

        # Get skills sorted alphabetically
        skill_names = []
        for skill in invention_data['skills']:
            skill_name = self.get_skill_name(skill['skillID'])
            if skill_name:
                skill_names.append(skill_name)
        skill_names.sort(key=str.lower)

        # Calculate required per run
        if is_tech3:
            # For Tech III, get the quantity from materials
            if invention_data['materials']:
                required_per_run = invention_data['materials'][0]['quantity'] // 3
            else:
                required_per_run = 0
        else:
            # For Tech II, get the quantity from materials
            if invention_data['materials']:
                required_per_run = invention_data['materials'][0]['quantity']
            else:
                required_per_run = 0

        lines = [
            f'ITEM: {name} ({type_id})',
            f'Group: {group_str}',
            f'Market Group: {market_group_str}',
            f'Tech Level: {tech_level}',
            '',
            'Invention:'
        ]

        if not is_tech3:
            # Tech II: Show base blueprint
            base_bp = invention_data.get('parent_bp')
            if base_bp:
                lines.append(f'Base: {base_bp["typeName"]} ({base_bp["typeID"]})')

        lines.append(f'Datacores: {", ".join(datacore_names)}')
        lines.append(f'Required Per Run: {required_per_run}')
        lines.append(f'Skills: {", ".join(skill_names)}')
        lines.append(f'Time: {invention_data["run_time_minutes"]}')

        if is_tech3:
            lines.append('Probabilities:')
            lines.append('| Invented From                | Decryptor                       | Probability | Runs | ME | TE |')
            lines.append('|------------------------------|---------------------------------|-------------|------|----|----|')

            relic_types = self._get_relic_type_for_product(item)
            base_skills = sum(s['level'] for s in invention_data['skills'])
            encryption_skill = invention_data['skill_level']  # Use the --skill value

            # Sort relics: Intact first, then Malfunctioning, then Wrecked
            relic_order = {'Intact': 0, 'Malfunctioning': 1, 'Wrecked': 2}
            relic_types.sort(key=lambda x: relic_order.get(x[2], 99))

            for relic_type_id, relic_name, relic_quality in relic_types:
                baseline = self.TECH3_RELIC_BASELINES[relic_quality]
                base_chance = baseline['base_chance']
                base_runs = baseline['base_runs']

                for decryptor_name, decryptor_mod in self.DECRYPTOR_MODIFIERS.items():
                    prob = self._calculate_success_probability(
                        base_chance, base_skills, encryption_skill, decryptor_mod, is_tech3
                    )
                    runs = base_runs + decryptor_mod['runs']
                    me = 2 + decryptor_mod['me']  # Base ME=2 for Tech III
                    te = 4 + decryptor_mod['te']  # Base TE=4 for Tech III

                    lines.append(
                        f'| {relic_name:<28} | {decryptor_name:<31} | {prob:>10.2f} | {runs:<4} | {me:<2} | {te:<2} |'
                    )
        else:
            lines.append('Probabilities:')
            lines.append('| Decryptor                       | Probability | Runs | ME | TE |')
            lines.append('|---------------------------------|-------------|------|----|----|')

            # Get base probability and runs from data
            prob_data = invention_data.get('base_probability', 0.0)
            base_runs = invention_data.get('base_runs', 10)

            # Calculate skill contributions: science skills and encryption
            # The --skill value applies to ALL relevant skills
            science_count = len(invention_data.get('science_skill_ids', []))
            encryption_count = 1 if invention_data.get('encryption_skill_id') else 0
            skill = invention_data.get('skill_level', 5)

            # Base skills from science (count only)
            base_skills = science_count * skill

            # Encryption skill (handled separately)
            encryption_skill = encryption_count * skill

            for decryptor_name, decryptor_mod in self.DECRYPTOR_MODIFIERS.items():
                prob = self._calculate_success_probability(
                    prob_data, base_skills, encryption_skill, decryptor_mod, is_tech3
                )
                runs = base_runs + decryptor_mod['runs']
                me = 2 + decryptor_mod['me']  # Base ME=2
                te = 4 + decryptor_mod['te']  # Base TE=4

                lines.append(
                    f'| {decryptor_name:<31} | {prob:>10.2f} | {runs:<4} | {me:<2} | {te:<2} |'
                )

        return '\n'.join(lines)

    def _calculate_success_probability(
        self, base_prob: float, base_skills: int, encryption: int,
        decryptor_mod: dict, is_tech3: bool
    ) -> float:
        """
        Calculate success probability using the formula:
        success = base * (1 + sum(science skills)/30 + encryption/40) * (1 + decryptor%/100)
        """
        prob = base_prob

        # Skill multiplier: (1 + sum(science skills)/30 + encryption/40)
        skill_multiplier = 1 + (base_skills / 30) + (encryption / 40)
        prob *= skill_multiplier

        # Decryptor multiplier: (1 + decryptor_probability_multiplier/100)
        decryptor_mult = decryptor_mod['probability_mult'] / 100
        prob *= (1 + decryptor_mult)

        # Clamp to [0, 1]
        prob = max(0.0, min(1.0, prob))

        return prob

    def generate_plan(self, build_string: str, config: dict) -> dict:
        """
        Generate a build plan for the given build string and config.
        Returns materials dict and jobs list.
        """
        parsed = self.parse_build_string(build_string)
        item_name = parsed['item_name']
        runs_per_job = parsed['runs_per_job']
        me = parsed['me']
        te = parsed['te']
        num_jobs = parsed['jobs']

        # Get TE bonus from config
        te_bonus = config.get('te_bonus', 0)

        if not 0 <= te_bonus <= 30:
            raise ValueError(f"TE bonus must be in range [0,30], got {te_bonus}")

        # Find the product
        product = self.find_product(item_name)
        if not product:
            # Try blueprint
            product = self.find_blueprint(item_name)

        if not product:
            # Try to find Tech II/III invention data
            invention_skill = config.get('invention_skill', 5)
            invention_data = self.get_invention_data(item_name, invention_skill)
            if invention_data:
                return self._generate_invention_plan(invention_data, parsed, config)
            raise ValueError(f"Could not find product or blueprint: {item_name}")

        is_blueprint = 'Blueprint' in product['typeName']

        if is_blueprint:
            # For blueprints, find what they manufacture
            product_type_id = None
            for (bp_id, act_id), products in self.industry_products.items():
                if bp_id == product['typeID'] and act_id == self.ACTIVITY_MANUFACTURING:
                    for p in products:
                        product_type_id = p['productTypeID']
                        break
                    break
            if not product_type_id:
                raise ValueError(f"Could not find product for blueprint: {item_name}")
        else:
            product_type_id = product['typeID']

        # Find blueprint for manufacturing
        bp = self.find_bp_for_product(product_type_id)
        if not bp:
            raise ValueError(f"Could not find blueprint for product: {item_name}")

        bp_type_id = bp['typeID']
        activity_key = (bp_type_id, self.ACTIVITY_MANUFACTURING)

        # Get recipe data
        activity_data = self.industry_activities.get(activity_key, {})
        bp_time_seconds = activity_data.get('time', 0)
        bp_time_minutes = math.ceil(bp_time_seconds / 60)

        # Calculate full TE factor
        # Full TE factor is BP Time * (1 - BP TE) * (1 - TE bonus)
        # But wait, we need to think about this differently
        # The TE bonus is applied on top of the blueprint's TE
        # So: adjusted_time = bp_time * (1 - te/100) * (1 - te_bonus/100)
        full_te_factor = (1 - me / 100) * (1 - te_bonus / 100)

        # Calculate actual run time with both ME and TE
        adjusted_time = bp_time_minutes * (1 - te / 100) * (1 - te_bonus / 100)
        adjusted_time = max(0, math.ceil(adjusted_time))

        # Get materials
        materials_list = self.industry_materials.get(activity_key, [])

        # Get output quantity per run
        output_per_run = 1
        if activity_key in self.industry_products:
            products = self.industry_products[activity_key]
            for p in products:
                output_per_run = p['quantity']
                break

        # Calculate total runs needed
        total_runs_needed = (runs_per_job * num_jobs + output_per_run - 1) // output_per_run

        # Calculate materials per run (with ME)
        materials_by_type = defaultdict(int)
        for mat in materials_list:
            mat_type_id = mat['materialTypeID']
            original_qty = mat['quantity']
            # ME reduces material needed
            adjusted_qty = math.ceil(max(1, original_qty * (1 - me / 100)))
            materials_by_type[mat_type_id] += adjusted_qty

        # Total materials for all runs
        total_materials = defaultdict(int)
        for mat_type_id, qty_per_run in materials_by_type.items():
            total_materials[mat_type_id] += qty_per_run * total_runs_needed

        # Build materials dict for CSV
        materials_for_csv = []
        for mat_type_id, total_qty in sorted(total_materials.items(), key=lambda x: self.get_item_name(x[0]).lower()):
            mat_name = self.get_item_name(mat_type_id) or f'Unknown ({mat_type_id})'
            buy_volume = math.ceil(total_qty / 100) * 100  # Assume volume of 100
            materials_for_csv.append({
                'name': mat_name,
                'to_buy': total_qty,
                'buy_volume': buy_volume,
                'start_amount': 0,  # Assume no starting materials
                'end_amount': 0
            })

        # Build jobs list
        jobs_for_csv = []
        job_name = product['typeName'].replace(' Blueprint', '') + ' Blueprint' if not is_blueprint else product['typeName']
        jobs_for_csv.append({
            'name': job_name,
            'runs': total_runs_needed,
            'days': adjusted_time / 1440,  # Convert minutes to days
            'count': num_jobs
        })

        return {
            'materials': materials_for_csv,
            'jobs': jobs_for_csv,
            'total_runs': total_runs_needed,
            'total_output': runs_per_job * num_jobs,
            'time_per_run_minutes': adjusted_time,
            'te': te,
            'te_bonus': te_bonus,
            'me': me
        }

    def _generate_invention_plan(self, invention_data: dict, parsed: dict, config: dict) -> dict:
        """Generate a plan for invention or reverse engineering."""
        runs_per_job = parsed['runs_per_job']
        num_jobs = parsed['jobs']
        decryptor_name = parsed['decryptor']
        relic_name = parsed['relic']

        is_tech3 = invention_data['is_tech3']

        # Get decryptor modifiers
        decryptor_mod = self.DECRYPTOR_MODIFIERS.get(decryptor_name, self.DECRYPTOR_MODIFIERS['None'])

        # Get base runs from invention data
        base_runs = invention_data.get('base_runs', 10)

        if is_tech3:
            # Tech III: use relic baselines
            # The runs per job is determined by relic type
            relic_name = relic_name or 'Intact'
            relic_key = relic_name
            if relic_key not in self.TECH3_RELIC_BASELINES:
                # Try to find matching relic
                for key in self.TECH3_RELIC_BASELINES:
                    if key in relic_name:
                        relic_key = key
                        break
            baseline = self.TECH3_RELIC_BASELINES.get(relic_key, self.TECH3_RELIC_BASELINES['Intact'])
            base_runs = baseline['base_runs']

        # Calculate runs with decryptor
        runs_with_decryptor = base_runs + decryptor_mod['runs']

        # Calculate total runs needed
        total_runs_needed = (runs_per_job * num_jobs + runs_with_decryptor - 1) // runs_with_decryptor

        # Get materials (datacores)
        materials_list = invention_data.get('materials', [])

        # If no materials on T2 blueprint, fallback to T1 blueprint
        if not materials_list:
            materials_list = self.industry_materials.get((invention_data.get('activity_type_id', 0), invention_data['activity_id']), [])

        materials_by_type = defaultdict(int)
        for mat in materials_list:
            mat_type_id = mat['materialTypeID']
            original_qty = mat['quantity']
            # For invention, the quantity is already per run
            materials_by_type[mat_type_id] += original_qty * total_runs_needed

        # Build materials dict for CSV
        materials_for_csv = []
        for mat_type_id, total_qty in sorted(materials_by_type.items(), key=lambda x: self.get_item_name(x[0]).lower()):
            mat_name = self.get_item_name(mat_type_id) or f'Unknown ({mat_type_id})'
            buy_volume = math.ceil(total_qty / 100) * 100
            materials_for_csv.append({
                'name': mat_name,
                'to_buy': total_qty,
                'buy_volume': buy_volume,
                'start_amount': 0,
                'end_amount': 0
            })

        # Add relic materials for Tech III
        if is_tech3 and relic_name:
            # Find relic type ID
            relic_type_id = None
            for name, tid in self.TECH3_RELIC_TYPES.items():
                if relic_name in name:
                    relic_type_id = tid
                    break
            if relic_type_id:
                # Calculate relic quantity needed
                relic_qty = total_runs_needed  # One relic per run
                found = False
                for m in materials_for_csv:
                    if m['name'] in ['Intact Hull Section', 'Malfunctioning Hull Section', 'Wrecked Hull Section',
                                      'Small Intact Hull Section', 'Small Malfunctioning Hull Section', 'Small Wrecked Hull Section']:
                        m['to_buy'] += relic_qty
                        m['buy_volume'] = math.ceil(m['to_buy'] / 100) * 100
                        found = True
                        break
                if not found:
                    relic_name_real = self.get_item_name(relic_type_id) or f'Relic ({relic_type_id})'
                    materials_for_csv.append({
                        'name': relic_name_real,
                        'to_buy': relic_qty,
                        'buy_volume': math.ceil(relic_qty / 100) * 100,
                        'start_amount': 0,
                        'end_amount': 0
                    })

        # Sort materials by name
        materials_for_csv.sort(key=lambda x: x['name'].lower())

        # Build jobs list
        jobs_for_csv = []
        job_name = invention_data['name'] + ' Blueprint' if 'Blueprint' not in invention_data['name'] else invention_data['name']

        # For invention, time is per run
        time_per_run = invention_data.get('run_time_minutes', 0)
        # TE bonus should be applied for invention too
        te_bonus = config.get('te_bonus', 0)
        adjusted_time = time_per_run * (1 - te_bonus / 100)

        jobs_for_csv.append({
            'name': 'Inventing ' + job_name,
            'runs': total_runs_needed,
            'days': math.ceil(adjusted_time) / 1440,
            'count': num_jobs
        })

        return {
            'materials': materials_for_csv,
            'jobs': jobs_for_csv,
            'total_runs': total_runs_needed,
            'total_output': runs_per_job * num_jobs,
            'time_per_run_minutes': math.ceil(adjusted_time),
            'te_bonus': te_bonus
        }

    def _split_job_for_duration(self, job_name: str, total_runs: int, days_per_run: float, split_days: float) -> list:
        """
        Split a job into multiple jobs if total duration exceeds split_days.
        Returns a list of job dicts (each with runs and days).
        """
        max_days = min(30, split_days)
        total_days = days_per_run * total_runs

        if total_runs <= 1 or total_days <= max_days:
            return [{'name': job_name, 'runs': total_runs, 'days': total_days, 'count': 1}]

        max_runs = int(split_days / days_per_run)
        if max_runs < 1:
            max_runs = 1

        N = total_runs

        t = math.ceil(N / max_runs)
        B = N // t
        x = N % t

        if B + 1 == max_runs and x > t / 2 and (N // (t + 1)) + 1 < max_runs:
            t = t + 1
            B = N // t
            x = N % t

        jobs = []
        # x jobs of (B+1)
        for _ in range(int(x)):
            runs = B + 1
            jobs.append({'name': job_name, 'runs': runs, 'days': days_per_run * runs, 'count': 1})
        # (t-x) jobs of B
        for _ in range(int(t - x)):
            runs = B
            jobs.append({'name': job_name, 'runs': runs, 'days': days_per_run * runs, 'count': 1})

        return jobs

    def _build_all_for_product(self, product_type_id: int, total_runs: int, me: int, te: int,
                               te_bonus: int, split_days: float, comp_me: int, comp_te: int,
                               visited: set, comp_defaults: dict) -> dict:
        """
        Recursively build all buildable components for a product.
        Returns a dict with materials, jobs, and any sub-components.
        Handles both manufacturing (ACTIVITY_MANUFACTURING) and reactions (ACTIVITY_REACTIONS).
        """
        all_materials = defaultdict(int)
        all_jobs = []

        # Try manufacturing first, fallback to reactions
        activities_to_try = []

        # Check if this product is manufactured
        bp = self.find_bp_for_product(product_type_id)
        if bp:
            activity_key = (bp['typeID'], self.ACTIVITY_MANUFACTURING)
            if activity_key in self.industry_materials or activity_key in self.industry_products:
                activities_to_try.append(('manufacturing', bp['typeID'], activity_key, me, te))

        # Check if this product is produced via reactions
        reaction_key = None
        for (type_id, activity_id) in self.industry_activities:
            if activity_id == self.ACTIVITY_REACTIONS:
                if type_id == product_type_id:
                    reaction_key = (type_id, activity_id)
                    # Also check products for this reaction
                    if reaction_key in self.industry_products:
                        activities_to_try.append(('reactions', type_id, reaction_key, 0, 0))
                    break

        if not activities_to_try:
            # No manufacturing or reactions found - just return materials to buy
            # This could be a raw material or non-buildable item
            mat_name = self.get_item_name(product_type_id) or f'Unknown ({product_type_id})'
            all_materials[mat_name] += total_runs
            return {'materials': dict(all_materials), 'jobs': all_jobs}

        # Process the first valid activity found (prefer manufacturing over reactions)
        activity_type, activity_type_id, activity_key, activity_me, activity_te = activities_to_try[0]

        # Get materials for this activity
        materials_list = self.industry_materials.get(activity_key, [])

        # Get activity time
        activity_data = self.industry_activities.get(activity_key, {})
        bp_time_seconds = activity_data.get('time', 0)
        bp_time_minutes = math.ceil(bp_time_seconds / 60)

        # Calculate adjusted time with ME/TE (for manufacturing)
        adjusted_time = bp_time_minutes
        if activity_type == 'manufacturing':
            adjusted_time = bp_time_minutes * (1 - activity_te / 100) * (1 - te_bonus / 100)
            adjusted_time = max(0, math.ceil(adjusted_time))
        else:
            # Reactions get TE bonus but not ME/TE from blueprint (ME doesn't affect reactions)
            adjusted_time = max(0, math.ceil(bp_time_minutes * (1 - te_bonus / 100)))

        days_per_run = adjusted_time / 1440

        # Get output per run
        output_per_run = 1
        if activity_key in self.industry_products:
            products = self.industry_products[activity_key]
            for p in products:
                output_per_run = p['quantity']
                break

        # Calculate total runs needed
        total_runs_needed = (total_runs + output_per_run - 1) // output_per_run

        # Check if T1 Capital ships (use ME=0, TE=0) - only for manufacturing
        item_name = self.get_item_name(product_type_id) or ''
        if activity_type == 'manufacturing' and 'Capital' in item_name and 'Blueprint' in item_name:
            activity_me = 0
            activity_te = 0

        # Process materials
        for mat in materials_list:
            mat_type_id = mat['materialTypeID']
            original_qty = mat['quantity']

            # Apply ME for manufacturing, but not for reactions
            if activity_type == 'manufacturing':
                adjusted_qty = math.ceil(max(1, original_qty * (1 - activity_me / 100)))
            else:
                adjusted_qty = original_qty

            total_mat_qty = adjusted_qty * total_runs_needed

            # Check if material is buildable
            if self.is_buildable(mat_type_id) and mat_type_id not in visited:
                visited.add(mat_type_id)
                # Recursively build this component
                sub_plan = self._build_all_for_product(
                    mat_type_id, total_mat_qty, comp_me, comp_te, te_bonus,
                    split_days, comp_me, comp_te, visited, comp_defaults
                )
                # Add sub jobs
                all_jobs.extend(sub_plan['jobs'])
                # Aggregate sub materials into our totals
                for mat_name, qty in sub_plan['materials'].items():
                    all_materials[mat_name] += qty
            else:
                # Material is not buildable or already visited, just add to materials to buy
                mat_name = self.get_item_name(mat_type_id) or f'Unknown ({mat_type_id})'
                all_materials[mat_name] += total_mat_qty

        # Create job entry for this product
        product_name = self.get_item_name(product_type_id) or f'Unknown ({product_type_id})'

        # For reactions, append " Reaction Formula" to match expected output
        if activity_type == 'reactions':
            if not product_name.endswith(' Reaction Formula'):
                product_name = product_name + ' Reaction Formula'
        else:
            # For manufacturing, ensure Blueprint suffix
            if 'Blueprint' not in product_name:
                # Try to find the blueprint name
                if bp:
                    product_name = bp['typeName']
                else:
                    product_name = product_name + ' Blueprint'

        split_jobs = self._split_job_for_duration(product_name, total_runs_needed, days_per_run, split_days)
        all_jobs.extend(split_jobs)

        return {
            'materials': dict(all_materials),
            'jobs': all_jobs
        }

    def generate_build_all_plan(self, build_string: str, config: dict) -> dict:
        """
        Generate a build plan that builds all buildable components.
        Uses split_days for job splitting and component_bpo_defaults for component ME/TE.
        """
        parsed = self.parse_build_string(build_string)
        item_name = parsed['item_name']
        runs_per_job = parsed['runs_per_job']
        me = parsed['me']
        te = parsed['te']
        num_jobs = parsed['jobs']

        # Get config values
        te_bonus = config.get('te_bonus', 0)
        split_days = config.get('split_days', 3.0)
        comp_defaults = config.get('component_bpo_defaults', {})
        comp_me = comp_defaults.get('me', 10)  # Default is 10 per spec
        comp_te = comp_defaults.get('te', 20)  # Default is 20 per spec

        # Find the product
        product = self.find_product(item_name)
        if not product:
            # Try blueprint
            product = self.find_blueprint(item_name)

        if not product:
            raise ValueError(f"Could not find product or blueprint: {item_name}")

        product_type_id = product['typeID']

        # Calculate total runs needed
        total_output = runs_per_job * num_jobs
        output_per_run = 1

        # Find the blueprint for this product
        bp = self.find_bp_for_product(product_type_id)
        if bp:
            activity_key = (bp['typeID'], self.ACTIVITY_MANUFACTURING)
            if activity_key in self.industry_products:
                products = self.industry_products[activity_key]
                for p in products:
                    output_per_run = p['quantity']
                    break

        total_runs_needed = (total_output + output_per_run - 1) // output_per_run

        # Build all components
        visited = {product_type_id}
        plan_data = self._build_all_for_product(
            product_type_id, total_runs_needed, me, te, te_bonus,
            split_days, comp_me, comp_te, visited, comp_defaults
        )

        # Consolidate jobs - group by name and sum runs/days
        consolidated_jobs = defaultdict(lambda: {'runs': 0, 'days': 0.0, 'count': 0})
        for job in plan_data['jobs']:
            name = job['name']
            consolidated_jobs[name]['runs'] += job['runs']
            consolidated_jobs[name]['days'] += job['days']
            consolidated_jobs[name]['count'] += job['count']

        # Convert to list format for CSV
        jobs_for_csv = []
        for name, data in consolidated_jobs.items():
            jobs_for_csv.append({
                'name': name,
                'runs': data['runs'],
                'days': data['days'],
                'count': data['count']
            })

        # Sort jobs by name alphabetically (case-insensitive)
        jobs_for_csv.sort(key=lambda x: x['name'].lower())

        # Build materials CSV from aggregated materials
        materials_for_csv = []
        for mat_name, total_qty in sorted(plan_data['materials'].items(),
                                          key=lambda x: x[0].lower()):
            buy_volume = math.ceil(total_qty / 100) * 100
            materials_for_csv.append({
                'name': mat_name,
                'to_buy': total_qty,
                'buy_volume': buy_volume,
                'start_amount': 0,
                'end_amount': 0
            })

        return {
            'materials': materials_for_csv,
            'jobs': jobs_for_csv,
            'total_runs': total_runs_needed,
            'total_output': total_output,
            'te': te,
            'te_bonus': te_bonus,
            'me': me
        }

    def write_plan_csvs(self, plan: dict, output_dir: str):
        """Write plan to CSV files in output directory."""
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        # Write materials.csv
        materials_path = output_path / 'materials.csv'
        with open(materials_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=['name', 'to_buy', 'buy_volume', 'start_amount', 'end_amount'])
            writer.writeheader()
            for row in plan['materials']:
                writer.writerow(row)

        # Write jobs.csv
        jobs_path = output_path / 'jobs.csv'
        with open(jobs_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=['name', 'runs', 'days', 'count'])
            writer.writeheader()
            for row in plan['jobs']:
                writer.writerow(row)


def main():
    parser = argparse.ArgumentParser(
        description='EVE Online Industry Recipe Planner',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''Examples:
  python industry.py recipe Naga --sde ./sde
  python industry.py recipe "Barrage L Blueprint" --sde ./sde
  python industry.py recipe Fernite Carbide --sde ./sde
  python industry.py invention Barrage L --sde ./sde --skill 5
  python industry.py invention "Proteus Blueprint" --sde ./sde --skill 5
  python industry.py plan "Barrage L 10 10 20 x10" --config config.yaml --sde ./sde --output ./output'''
    )

    subparsers = parser.add_subparsers(dest='command', required=True)

    recipe_parser = subparsers.add_parser('recipe', help='Generate recipe for a product or blueprint')
    recipe_parser.add_argument('name', help='Product or Blueprint name (exact, case-sensitive)')
    recipe_parser.add_argument('--sde', required=True, help='Path to SDE directory')
    recipe_parser.add_argument('--material-efficiency', '-me', type=int, choices=range(0, 11), default=0,
                                   help='Material Efficiency integer in range [0,10] (default: 0)')
    recipe_parser.add_argument('--time-efficiency', '-te', type=int, choices=range(0, 21), default=0,
                                   help='Time Efficiency even integer in range [0,20] (default: 0)')
    recipe_parser.add_argument('--display-waste', '-waste', action='store_true',
                                   help='Display waste due to ME not being max')

    invention_parser = subparsers.add_parser('invention', help='Generate invention report for a Tech II/III product or blueprint')
    invention_parser.add_argument('name', help='Product or Blueprint name (exact, case-sensitive)')
    invention_parser.add_argument('--sde', required=True, help='Path to SDE directory')
    invention_parser.add_argument('--skill', type=int, choices=range(1, 6), default=5,
                                   help='Skill level for science and encryption skills (default: 5)')

    plan_parser = subparsers.add_parser('plan', help='Create a build plan for a given item')
    plan_parser.add_argument('build_string', help='Build string in one of 4 formats (see documentation)')
    plan_parser.add_argument('--config', required=True, help='Path to YAML build config file')
    plan_parser.add_argument('--sde', required=True, help='Path to SDE directory')
    plan_parser.add_argument('--output', required=True, help='Path to output directory')
    plan_parser.add_argument('--build-all', action='store_true',
                               help='Build all buildable components for the product (enables job splitting based on split_days)')
    plan_parser.add_argument('--stocks', help='Path to text file with current stockpile items (format: {Item Name} {Quantity})')
    plan_parser.add_argument('--targets', help='Path to text file with stock target levels (format: {Item Name} {Quantity})')

    args = parser.parse_args()

    # Initialize SDE parser
    sde = SDEParser(args.sde)
    sde.load_all()

    # Initialize planner
    planner = IndustryPlanner(sde)

    if args.command == 'recipe':
        # Validate TE is even
        if args.time_efficiency % 2 != 0:
            print(f'Error: Time Efficiency must be an even integer', file=sys.stderr)
            sys.exit(1)

        recipe = planner.get_recipe_for_product(
            args.name,
            material_efficiency=args.material_efficiency,
            time_efficiency=args.time_efficiency,
            display_waste=args.display_waste
        )

        if not recipe:
            print(f'Error: Could not find product or blueprint "{args.name}"', file=sys.stderr)
            sys.exit(1)

        output = planner.format_recipe(recipe)
        print(output)

    elif args.command == 'invention':
        invention_data = planner.get_invention_data(args.name, args.skill)

        if not invention_data:
            print(f'Error: Could not find Tech II or Tech III invention data for "{args.name}"', file=sys.stderr)
            sys.exit(1)

        output = planner.format_invention_report(invention_data)
        print(output)

    elif args.command == 'plan':
        # Load config
        config = planner.load_config(args.config)

        # Validate TE bonus in config
        te_bonus = config.get('te_bonus', 0)
        if not 0 <= te_bonus <= 30:
            print(f'Error: TE bonus must be in range [0,30], got {te_bonus}', file=sys.stderr)
            sys.exit(1)

        # Validate invention skill in config
        invention_skill = config.get('invention_skill', 5)
        if not 1 <= invention_skill <= 5:
            print(f'Error: Invention skill must be in range [1,5], got {invention_skill}', file=sys.stderr)
            sys.exit(1)

        # Validate split_days in config (new field)
        split_days = config.get('split_days', 3.0)
        if not 0 < split_days <= 30:
            print(f'Error: split_days must be in range (0,30], got {split_days}', file=sys.stderr)
            sys.exit(1)

        # Validate component_bpo_defaults in config (new field)
        comp_defaults = config.get('component_bpo_defaults', {})
        if 'me' in comp_defaults:
            me = comp_defaults['me']
            if not 0 <= me <= 10:
                print(f'Error: component_bpo_defaults.me must be in range [0,10], got {me}', file=sys.stderr)
                sys.exit(1)
        if 'te' in comp_defaults:
            te = comp_defaults['te']
            if not 0 <= te <= 20 or te % 2 != 0:
                print(f'Error: component_bpo_defaults.te must be even integer in range [0,20], got {te}', file=sys.stderr)
                sys.exit(1)

        # Load stocks and targets if provided
        stocks = {}
        targets = {}
        if args.stocks:
            stocks = planner.load_stock_file(args.stocks)
        if args.targets:
            targets = planner.load_stock_file(args.targets)

        # Get config values
        te_bonus = config.get('te_bonus', 0)
        invention_skill = config.get('invention_skill', 5)
        comp_defaults = config.get('component_bpo_defaults', {})
        comp_me = comp_defaults.get('me', 10)
        comp_te = comp_defaults.get('te', 20)

        # Generate plan
        try:
            if args.build_all and targets:
                # Use the new method that handles stocks and targets together
                plan = planner.generate_plan_with_stocks_and_targets(args.build_string, config, stocks, targets)
            else:
                plan = planner.generate_build_all_plan(args.build_string, config)
        except ValueError as e:
            print(f'Error: {e}', file=sys.stderr)
            sys.exit(1)

        # Write CSV files
        planner.write_plan_csvs(plan, args.output)

        print(f'Plan generated successfully in {args.output}')
        print(f'  Materials: {args.output}/materials.csv')
        print(f'  Jobs: {args.output}/jobs.csv')


if __name__ == '__main__':
    main()
