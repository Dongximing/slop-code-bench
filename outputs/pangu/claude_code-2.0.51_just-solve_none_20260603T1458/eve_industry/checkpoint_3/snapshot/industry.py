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


def main():
    parser = argparse.ArgumentParser(
        description='EVE Online Industry Recipe Planner',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''Examples:
  python industry.py recipe Naga --sde ./sde
  python industry.py recipe "Barrage L Blueprint" --sde ./sde
  python industry.py recipe Fernite Carbide --sde ./sde
  python industry.py invention Barrage L --sde ./sde --skill 5
  python industry.py invention "Proteus Blueprint" --sde ./sde --skill 5'''
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


if __name__ == '__main__':
    main()
