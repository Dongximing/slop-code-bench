#!/usr/bin/env python3
"""
Reprocessing API - EVE Online item reprocessing calculator.
"""

import gzip
import csv
import io
import os
import json
import hashlib
import secrets
import base64
from datetime import datetime
from collections import defaultdict
from typing import Dict, List, Optional, Any, Tuple
from functools import lru_cache

from flask import Flask, request, jsonify
from flask_restful import Api, Resource
import yaml

from market_tools import app, api
from market_tools import price_data, stations, id_to_name, name_to_id


# ============================================================================
# SDE Data Structures
# ============================================================================

class ReprocessingSDELoader:
    """Extended SDE loader for reprocessing data."""

    # Category IDs
    CATEGORY_ORE = 25          # Asteroid category
    CATEGORY_ICE = 423         # Ice Products (checking)
    CATEGORY_GAS = 24          # Reaction category for gas
    CATEGORY_MATERIAL = 4      # Minerals/Commodities
    CATEGORY_COMMODITY = 17    # Commodities
    CATEGORY_SHIP = 6          # Ships
    CATEGORY_MODULE = 7        # Modules
    CATEGORY_PI_RESOURCE = 42  # Planetary Resources
    CATEGORY_PI_COMMODITY = 43 # Planetary Commodities

    # Group IDs
    GROUP_ICE = 465

    # Gas types (based on dgmTypeAttributes search)
    GAS_TYPES = {1140, 11140, 11141, 11142}  # Common gases

    def __init__(self, sde_path: str):
        self.sde_path = sde_path
        self.load_sde()

    def load_sde(self):
        """Load all SDE data from compressed CSV files."""
        # Load invTypes
        types_file = os.path.join(self.sde_path, "invTypes.csv.bz2")
        if os.path.exists(types_file):
            self._load_types(types_file)
        else:
            # Fallback to already loaded data
            pass

        # Load invTypeMaterials
        materials_file = os.path.join(self.sde_path, "invTypeMaterials.csv.bz2")
        if os.path.exists(materials_file):
            self._load_materials(materials_file)
        else:
            self.type_materials = {}

        # Load invCategories
        categories_file = os.path.join(self.sde_path, "invCategories.csv.bz2")
        if os.path.exists(categories_file):
            self._load_categories(categories_file)
        else:
            self.category_map = {}

        # Load invGroups
        groups_file = os.path.join(self.sde_path, "invGroups.csv.bz2")
        if os.path.exists(groups_file):
            self._load_groups(groups_file)
        else:
            self.group_map = {}

        # Load dgmTypeAttributes for ore/moon/gas processing skill reference
        attributes_file = os.path.join(self.sde_path, "dgmTypeAttributes.csv.bz2")
        if os.path.exists(attributes_file):
            self._load_attributes(attributes_file)
        else:
            self.item_ore_processing_skill = {}

        # Load ship volumes
        ship_volumes_file = os.path.join(self.sde_path, "ship_volumes.yaml")
        if os.path.exists(ship_volumes_file):
            self._load_ship_volumes(ship_volumes_file)
        else:
            self.ship_volumes = {}

        print(f"Reprocessing SDE loaded: {len(self.type_materials)} materials, {len(self.category_map)} categories")

    def _load_types(self, filepath: str):
        """Load type names, IDs, and metadata from invTypes.csv."""
        try:
            import bz2
            with bz2.open(filepath, 'rt', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    type_id = int(row['typeID'])
                    name = row.get('typeName', '')
                    group_id = int(row.get('groupID', 0)) if row.get('groupID') else 0

                    # Parse numeric fields
                    mass = float(row.get('mass', 0)) if row.get('mass') else 0.0
                    volume = float(row.get('volume', 0)) if row.get('volume') else 0.0
                    capacity = float(row.get('capacity', 0)) if row.get('capacity') else 0.0
                    portion_size = int(row.get('portionSize', 1)) if row.get('portionSize') else 1
                    published = row.get('published', '0') == '1'

                    if name:
                        global id_to_name, name_to_id
                        id_to_name[type_id] = name
                        name_to_id[name] = type_id

                        # Store type metadata
                        self.type_metadata[type_id] = {
                            'name': name,
                            'groupID': group_id,
                            'mass': mass,
                            'volume': volume,
                            'capacity': capacity,
                            'portionSize': portion_size,
                            'published': bool(published)
                        }
        except Exception as e:
            raise ValueError(f"Error loading types file: {e}")

    def _load_materials(self, filepath: str):
        """Load material conversion data from invTypeMaterials.csv."""
        self.type_materials = {}
        try:
            import bz2
            with bz2.open(filepath, 'rt', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    type_id = int(row['typeID'])
                    material_id = int(row['materialTypeID'])
                    quantity = int(row['quantity'])

                    if type_id not in self.type_materials:
                        self.type_materials[type_id] = []

                    self.type_materials[type_id].append({
                        'materialTypeID': material_id,
                        'quantity': quantity
                    })
        except Exception as e:
            raise ValueError(f"Error loading materials file: {e}")

    def _load_categories(self, filepath: str):
        """Load category mapping from invCategories.csv."""
        self.category_map = {}
        try:
            import bz2
            with bz2.open(filepath, 'rt', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    cat_id = int(row['categoryID'])
                    cat_name = row.get('categoryName', '')
                    if cat_name:
                        self.category_map[cat_id] = cat_name
        except Exception as e:
            raise ValueError(f"Error loading categories file: {e}")

    def _load_groups(self, filepath: str):
        """Load group-to-category mapping from invGroups.csv."""
        self.group_map = {}
        try:
            import bz2
            with bz2.open(filepath, 'rt', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    group_id = int(row['groupID'])
                    cat_id = int(row.get('categoryID', 0)) if row.get('categoryID') else 0
                    group_name = row.get('groupName', '')

                    if group_name:
                        self.group_map[group_id] = {
                            'name': group_name,
                            'categoryID': cat_id
                        }
        except Exception as e:
            raise ValueError(f"Error loading groups file: {e}")

    def _load_attributes(self, filepath: str):
        """Load attributes from dgmTypeAttributes.csv."""
        # Ore/moon/gas processing skill reference (attributeID 1140 = refinement type)
        # We'll map items to their ore/gas processing skill requirements
        self.item_ore_processing_skill = {}

        try:
            import bz2
            with bz2.open(filepath, 'rt', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    type_id = int(row.get('typeID', 0))
                    attr_id = int(row.get('attributeID', 0)) if row.get('attributeID') else 0
                    val_int = row.get('valueInt')
                    val_float = row.get('valueFloat')

                    # Attribute 1140 indicates what skill type is needed for ore processing
                    # We'll store this as a reference
                    if attr_id == 1140:
                        value = int(val_int) if val_int else float(val_float) if val_float else 0
                        self.item_ore_processing_skill[type_id] = value
        except Exception as e:
            print(f"Warning: Error loading attributes file: {e}")

    def _load_ship_volumes(self, filepath: str):
        """Load ship packaged volumes from YAML file."""
        self.ship_volumes = {}
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
                if data:
                    for item, volume_str in data.items():
                        # Parse volume string like "2.5K m3"
                        volume_str = volume_str.strip()
                        if 'K' in volume_str:
                            volume = float(volume_str.replace('K', '').replace('m3', '').strip()) * 1000
                        else:
                            volume = float(volume_str.replace('m3', '').strip())
                        self.ship_volumes[item] = volume
        except Exception as e:
            print(f"Warning: Error loading ship volumes: {e}")

    def get_type_id(self, name: str) -> Optional[int]:
        """Get type ID from name."""
        return name_to_id.get(name)

    def get_type_name(self, type_id: int) -> str:
        """Get type name from ID."""
        return id_to_name.get(type_id, f"Unknown {type_id}")

    def get_materials(self, type_id: int) -> List[Dict]:
        """Get materials for a type."""
        return self.type_materials.get(type_id, [])

    def has_materials(self, type_id: int) -> bool:
        """Check if a type has materials (is reprocessable)."""
        return type_id in self.type_materials

    def get_volume(self, type_id: int, quantity: int = 1) -> float:
        """Get total volume for a quantity of an item."""
        # Check ship volumes first
        type_name = self.get_type_name(type_id)
        if type_name in self.ship_volumes:
            return self.ship_volumes[type_name] * quantity

        # Get from type metadata
        metadata = self.type_metadata.get(type_id, {})
        item_volume = metadata.get('volume', 0.0) or 0.0
        return item_volume * quantity

    def get_portion_size(self, type_id: int) -> int:
        """Get portion size for reprocessing."""
        metadata = self.type_metadata.get(type_id, {})
        return metadata.get('portionSize', 1) or 1

    def get_category(self, type_id: int) -> str:
        """Get category name for a type."""
        metadata = self.type_metadata.get(type_id, {})
        group_id = metadata.get('groupID', 0)
        group_info = self.group_map.get(group_id, {})
        cat_id = group_info.get('categoryID', 0)
        return self.category_map.get(cat_id, '')

    def is_ore_or_ice(self, type_id: int) -> bool:
        """Check if item is ore or ice (compressed variants included)."""
        cat_name = self.get_category(type_id).lower()
        group_name = self.group_map.get(
            self.type_metadata.get(type_id, {}).get('groupID', 0), {}
        ).get('name', '').lower()

        # Ore category
        if cat_name == 'asteroid':
            return True
        # Ice group
        if group_name == 'ice':
            return True
        # Ice Product category (compressed ice)
        if cat_name == 'ice product':
            return True

        return False

    def is_gas(self, type_id: int) -> bool:
        """Check if item is gas (compressed variants included)."""
        # Check against known gas types
        if type_id in self.GAS_TYPES:
            return True
        # Gas category
        cat_name = self.get_category(type_id).lower()
        if cat_name == 'reaction' or cat_name == 'gas':
            return True
        return False

    def is_scrap(self, type_id: int) -> bool:
        """Check if item is scrap (everything except ore, ice, gas)."""
        return not (self.is_ore_or_ice(type_id) or self.is_gas(type_id))

    def get_ore_processing_skill(self, type_id: int) -> int:
        """Get the ore/moon/gas processing skill ID for an item."""
        return self.item_ore_processing_skill.get(type_id, 0)

    # Initialize type metadata dict
    type_metadata: Dict[int, Dict] = {}


# ============================================================================
# API Key Management
# ============================================================================

API_KEYS: Dict[str, Dict] = {}


def generate_api_key(config: Dict) -> str:
    """Generate a unique API key from config."""
    # Create a deterministic hash of the config
    config_str = json.dumps(config, sort_keys=True)
    hash_input = config_str + secrets.token_hex(8)
    key_hash = hashlib.sha256(hash_input.encode()).hexdigest()[:32]
    return key_hash


def parse_api_key(key: str) -> Optional[Dict]:
    """Parse API key to get configuration."""
    return API_KEYS.get(key)


def validate_structure_config(structure: Optional[Dict]) -> Optional[Dict]:
    """Validate structure configuration."""
    if not structure:
        return None

    valid_types = ["npc_station", "citadel", "athanor", "tatara"]
    valid_rigs = [None, "t1", "t2"]
    valid_security = ["highsec", "lowsec", "nullsec"]

    errors = []
    result = {}

    # Validate type
    stype = structure.get('type', 'npc_station')
    if stype not in valid_types:
        errors.append(f"Invalid structure type: {stype}")
    result['type'] = stype

    # Validate rig (npc_station must use rig=null)
    rig = structure.get('rig', None)
    if stype == "npc_station" and rig is not None:
        errors.append("npc_station cannot specify a rig")
        rig = None
    if rig is not None and rig not in valid_rigs:
        errors.append(f"Invalid rig value: {rig}")
        rig = None
    result['rig'] = rig

    # Validate security
    security = structure.get('security', 'highsec')
    if security not in valid_security:
        errors.append(f"Invalid security value: {security}")
    result['security'] = security

    if errors:
        return {"error": True, "details": errors}

    return result


def validate_skills(skills: Optional[Dict]) -> Optional[Dict]:
    """Validate skills configuration."""
    if not skills:
        return None

    valid_skills = ["Reprocessing", "Reprocessing Efficiency", "Ore Processing",
                    "Gas Cloud Harvesting", "Arkonor Processing", "Bistot Processing",
                    "Crokite Processing", "Dark Ochre Processing", "Spodumain Processing",
                    "Kernite Processing", "Omber Processing", "Plagioclase Processing",
                    "Jaspet Processing", "Hemorphite Processing", "Gneiss Processing",
                    "Hedbergite Processing", "Veldspar Processing"]

    result = {}
    for skill, level in skills.items():
        if not isinstance(level, int) or level < 0 or level > 5:
            return {"error": True, "details": [f"Skill level must be integer 0-5, got: {level}" for _ in [skill]]}
        result[skill] = level

    return result


def validate_implant(implant: Optional[str]) -> Optional[str]:
    """Validate implant configuration."""
    if not implant:
        return None

    valid_implants = [None, "RX-801", "RX-802", "RX-804"]
    if implant not in valid_implants:
        return {"error": True, "details": [f"Invalid implant: {implant}"]} if isinstance(implant, str) else None

    return implant


def calculate_yield_config(config: Dict) -> Dict:
    """Calculate yield percentages from configuration."""
    result = {'ore': 0.9063, 'gas': 0.95, 'scrap': 0.55}

    # Structure modifiers
    sm_map = {
        "npc_station": 0.00,
        "citadel": 0.00,
        "athanor": 0.02,
        "tatara": 0.055
    }

    # Rig modifiers (Rm)
    rig_map = {
        None: 0,
        "t1": 1,
        "t2": 3
    }

    # Security modifiers (Sec)
    sec_map = {
        "highsec": 0.00,
        "lowsec": 0.06,
        "nullsec": 0.12
    }

    # Implant modifiers (Im)
    implant_map = {
        None: 0.00,
        "RX-801": 0.01,
        "RX-802": 0.02,
        "RX-804": 0.04
    }

    structure = config.get('structure', {})
    skills = config.get('skills', {})
    implant = config.get('implant', None)

    # Get structure type (default npc_station)
    stype = structure.get('type', 'npc_station')

    # Get rig (default null, npc_station can't have rig)
    rig = structure.get('rig', None) if stype != 'npc_station' else None

    # Get security (default highsec)
    security = structure.get('security', 'highsec')

    # Calculate base 50 + skill
    base_skill = skills.get("Reprocessing", 0)
    if stype == "npc_station":
        base_yield = 50 + base_skill
    elif stype == "citadel":
        base_yield = 50 + base_skill
    elif stype == "athanor":
        base_yield = 50 + base_skill
    elif stype == "tatara":
        base_yield = 50 + base_skill
    else:
        base_yield = 50 + base_skill

    # Get modifiers
    sm = sm_map.get(stype, 0.00)
    rm = rig_map.get(rig, 0)
    sec = sec_map.get(security, 0.00)

    # Get skill levels
    reprocess_skill = skills.get("Reprocessing", 0)
    reprocess_eff_skill = skills.get("Reprocessing Efficiency", 0)
    op_skill = skills.get("Ore Processing", 0)  # Default for ore

    # Get implant modifier
    im = implant_map.get(implant, 0.00)

    # Calculate ore yield
    # Yield = (50 + Rm) * (1 + Sec) * (1 + Sm) * (1 + R*0.03) * (1 + Re*0.02) * (1 + Op*0.02) * (1 + Im)
    ore_yield = (50 + rm) * (1 + sec) * (1 + sm) * \
                (1 + reprocess_skill * 0.03) * (1 + reprocess_eff_skill * 0.02) * \
                (1 + op_skill * 0.02) * (1 + im)

    # Gas uses same formula but different default
    gas_yield = (50 + rm) * (1 + sec) * (1 + sm) * \
                (1 + reprocess_skill * 0.03) * (1 + reprocess_eff_skill * 0.02) * \
                (1 + op_skill * 0.02) * (1 + im)

    # Scrap gets 50 + scrap reprocessing skill (max 55)
    scrap_yield = 50 + skills.get("Reprocessing", 0)
    scrap_yield = min(scrap_yield, 55)

    # Cap ore/gas at 100%
    ore_yield = min(ore_yield, 100.0)
    gas_yield = min(gas_yield, 100.0)
    scrap_yield = min(scrap_yield, 55.0)

    result['ore'] = round(ore_yield, 2) / 100
    result['gas'] = round(gas_yield, 2) / 100
    result['scrap'] = round(scrap_yield, 2) / 100

    return result


class ReprocessingEngine:
    """Core reprocessing engine."""

    def __init__(self, sde: ReprocessingSDELoader):
        self.sde = sde

    def reprocess_item(self, type_id: int, quantity: int, yield_pct: float,
                       drill_down: bool = False) -> Tuple[Dict, float, List[Dict]]:
        """
        Reprocess an item and return materials.

        Returns:
            Tuple of (leftover_products, output_volume, reprocessed_materials)
        """
        # Check if item has materials
        if not self.sde.has_materials(type_id):
            # Non-reprocessable item - return unchanged
            volume = self.sde.get_volume(type_id, quantity)
            return {
                type_id: {'quantity': quantity, 'volume': round(volume, 2)}
            }, volume, []

        portion_size = self.sde.get_portion_size(type_id)
        full_portions = quantity // portion_size
        leftovers = quantity % portion_size

        materials = self.sde.get_materials(type_id)

        # Calculate total materials from all full portions
        total_materials = defaultdict(int)
        for mat in materials:
            mat_id = mat['materialTypeID']
            qty = mat['quantity'] * full_portions
            total_materials[mat_id] += qty

        # Apply efficiency and round down
        output_materials = []
        for mat_id, total_qty in total_materials.items():
            final_qty = int(total_qty * yield_pct)
            if final_qty > 0:
                output_materials.append({
                    'type_id': mat_id,
                    'quantity': final_qty
                })

        # Calculate leftover volume
        leftover_volume = self.sde.get_volume(type_id, leftovers)

        # Build product dict
        products = {}
        output_volume = 0.0

        # Add leftover items
        if leftovers > 0:
            products[type_id] = {'quantity': leftovers, 'volume': round(leftover_volume, 2)}
            output_volume += leftover_volume

        # Add reprocessed materials
        reprocessed_list = []
        for mat in output_materials:
            mat_id = mat['type_id']
            mat_qty = mat['quantity']
            mat_volume = self.sde.get_volume(mat_id, mat_qty)

            products[mat_id] = {
                'quantity': mat_qty,
                'volume': round(mat_volume, 2)
            }
            output_volume += mat_volume

            reprocessed_list.append({
                'type_id': mat_id,
                'quantity': mat_qty
            })

        # If drill_down is enabled, recursively process reprocessed materials
        if drill_down:
            drill_down_products = {}
            drill_down_volume = 0.0
            additional_reprocessed = []

            for mat_id, mat_data in products.items():
                if self.sde.has_materials(mat_id):
                    # Recursively reprocess with same yield
                    sub_leftovers, sub_volume, sub_reprocessed = self.reprocess_item(
                        mat_id, mat_data['quantity'], yield_pct, drill_down=False
                    )
                    # Merge results
                    for lid, ldata in sub_leftovers.items():
                        if lid in drill_down_products:
                            drill_down_products[lid]['quantity'] += ldata['quantity']
                            drill_down_products[lid]['volume'] = round(
                                drill_down_products[lid]['quantity'] *
                                (self.sde.get_volume(lid, 1) or 0.0), 2
                            )
                        else:
                            drill_down_products[lid] = ldata
                    drill_down_volume += sub_volume
                    additional_reprocessed.extend(sub_reprocessed)
                else:
                    drill_down_products[mat_id] = mat_data
                    drill_down_volume += mat_data['volume']

            products = drill_down_products
            output_volume = drill_down_volume

        return products, round(output_volume, 2), reprocessed_list


# ============================================================================
# API Endpoints
# ============================================================================

class ReprocessConfig(Resource):
    """Handle configuration-driven yield API key creation."""

    def post(self):
        """Create a new API key with configuration-driven yields."""
        try:
            data = request.get_json()
            if not data:
                return {"error": "INVALID_FORMAT", "details": "No JSON data provided"}, 400
        except Exception:
            return {"error": "INVALID_FORMAT", "details": "Invalid JSON"}, 400

        structure = data.get('structure')
        skills = data.get('skills')
        implant = data.get('implant')

        # Validate structure
        structure_result = validate_structure_config(structure)
        if structure_result and structure_result.get('error'):
            error_details = structure_result.get('details', ['Invalid structure'])[0]
            return {
                "code": "INVALID_VALUE",
                "path": "structure",
                "message": f"Invalid structure: {error_details}"
            }, 404

        # Validate skills
        if skills:
            skills_result = validate_skills(skills)
            if skills_result and skills_result.get('error'):
                return {
                    "code": "INVALID_VALUE",
                    "path": "skills",
                    "message": skills_result['details'][0] if skills_result.get('details') else "Invalid skills"
                }, 404

        # Validate implant
        if implant:
            implant_result = validate_implant(implant)
            if isinstance(implant_result, dict) and implant_result.get('error'):
                return {
                    "code": "INVALID_VALUE",
                    "path": "implant",
                    "message": "Invalid implant value"
                }, 404

        # Build config and generate key
        config = {
            'structure': structure_result if structure else {},
            'skills': skills or {},
            'implant': implant
        }

        # Calculate yields for this config
        yields = calculate_yield_config(config)
        config['yields'] = yields

        # Generate API key
        api_key = generate_api_key(config)
        API_KEYS[api_key] = config

        return {"key": api_key}, 201


class ReprocessEndpoint(Resource):
    """Handle reprocessing requests."""

    def post(self):
        """Process reprocessing request."""
        global sde_loader

        try:
            data = request.get_json()
            if not data:
                return {"error": "INVALID_FORMAT", "details": "No JSON data provided"}, 400
        except Exception:
            return {"error": "INVALID_FORMAT", "details": "Invalid JSON"}, 400

        items = data.get('items', {})
        if not items:
            return {"error": "INVALID_FORMAT", "details": "No items provided"}, 400

        efficiency = data.get('efficiency', {})
        drill_down = data.get('drill_down', False)
        market = data.get('market')

        # Validate items
        item_quantities = {}
        unknown_items = []
        total_input_volume = 0.0

        for item_name, quantity in items.items():
            type_id = sde_loader.get_type_id(item_name)
            if type_id is None:
                unknown_items.append(item_name)
                continue

            if not isinstance(quantity, int) or quantity <= 0:
                return {
                    "error": "INVALID_FORMAT",
                    "details": f"Quantity must be positive integer, got: {quantity}"
                }, 400

            item_quantities[type_id] = quantity
            total_input_volume += sde_loader.get_volume(type_id, quantity)

        # Return 404 for unknown items
        if unknown_items:
            return {
                "error": "UNKNOWN_ITEMS",
                "details": sorted(unknown_items)
            }, 404

        # Determine yields
        yields = {'ore': 0.9063, 'gas': 0.95, 'scrap': 0.55}

        # Check for API key
        api_key = request.headers.get('X-API-Key')
        if api_key:
            config = parse_api_key(api_key)
            if not config:
                return {"code": "BAD_API_KEY", "message": "Invalid API key"}, 401

            # Use config-driven yields
            config_yields = config.get('yields', yields)
            yields = config_yields.copy()
        else:
            # Use efficiency values
            if 'ore' in efficiency:
                yields['ore'] = float(efficiency['ore'])
            if 'gas' in efficiency:
                yields['gas'] = float(efficiency['gas'])
            if 'scrap' in efficiency:
                yields['scrap'] = float(efficiency['scrap'])

        # Initialize reprocessing engine
        engine = ReprocessingEngine(sde_loader)

        # Track output volumes
        total_output_volume = 0.0
        products_output = {}
        tracked_yields = {'ore': None, 'gas': None, 'scrap': None}

        # Process each item
        for type_id, quantity in item_quantities.items():
            # Determine which yield to use
            if sde_loader.is_ore_or_ice(type_id):
                yield_pct = yields['ore']
                tracked_yields['ore'] = yields['ore']
            elif sde_loader.is_gas(type_id):
                yield_pct = yields['gas']
                tracked_yields['gas'] = yields['gas']
            else:
                yield_pct = yields['scrap']
                tracked_yields['scrap'] = yields['scrap']

            # Reprocess item
            item_products, item_output_volume, _ = engine.reprocess_item(
                type_id, quantity, yield_pct, drill_down
            )

            # Merge products
            for prod_id, prod_data in item_products.items():
                if prod_id in products_output:
                    products_output[prod_id]['quantity'] += prod_data['quantity']
                    products_output[prod_id]['volume'] = round(
                        products_output[prod_id]['quantity'] *
                        (sde_loader.get_volume(prod_id, 1) or 0.0), 2
                    )
                else:
                    products_output[prod_id] = prod_data

            total_output_volume += item_output_volume

        # Build response
        result = {
            "inputs": {
                "volume": round(total_input_volume, 2),
                "buy": None,
                "sell": None,
                "buy_5pct": None,
                "sell_5pct": None,
            },
            "outputs": {
                "volume": round(total_output_volume, 2),
                "buy": None,
                "sell": None,
                "buy_5pct": None,
                "sell_5pct": None,
            },
            "yields": tracked_yields,
            "products": {}
        }

        # Add market data if available
        if market and market != 'null' and market in ['jita', 'amarr', 'dodixie', 'rens', 'hek']:
            # Get prices for products
            market_prices = self._get_market_prices(products_output.keys(), market)

            inputs_buy = 0.0
            inputs_sell = 0.0
            outputs_buy = 0.0
            outputs_sell = 0.0

            for type_id, quantity in item_quantities.items():
                prices = market_prices.get(type_id, {})
                if prices:
                    inputs_buy += prices.get('buy', 0) * quantity
                    inputs_sell += prices.get('sell', 0) * quantity

            for prod_id, prod_data in products_output.items():
                prices = market_prices.get(prod_id, {})
                if prices:
                    outputs_buy += prices.get('buy', 0) * prod_data['quantity']
                    outputs_sell += prices.get('sell', 0) * prod_data['quantity']

                result['products'][sde_loader.get_type_name(prod_id)] = {
                    'quantity': prod_data['quantity'],
                    'volume': prod_data['volume'],
                    'buy': prices.get('buy'),
                    'sell': prices.get('sell')
                }

            result['inputs']['buy'] = round(inputs_buy, 2)
            result['inputs']['sell'] = round(inputs_sell, 2)
            result['inputs']['buy_5pct'] = result['inputs']['buy']
            result['inputs']['sell_5pct'] = result['inputs']['sell']
            result['outputs']['buy'] = round(outputs_buy, 2)
            result['outputs']['sell'] = round(outputs_sell, 2)
            result['outputs']['buy_5pct'] = result['outputs']['buy']
            result['outputs']['sell_5pct'] = result['outputs']['sell']
        else:
            # No market data
            for prod_id, prod_data in products_output.items():
                result['products'][sde_loader.get_type_name(prod_id)] = {
                    'quantity': prod_data['quantity'],
                    'volume': prod_data['volume'],
                    'buy': None,
                    'sell': None
                }

        return result, 201

    def _get_market_prices(self, type_ids: List[int], market: str) -> Dict:
        """Get market prices for type IDs."""
        prices = {}

        # Map market name to location ID
        market_hubs = {
            'jita': 60003761,
            'amarr': 60008494,
            'dodixie': 60014718,
            'rens': 60004588,
            'hek': 60005614
        }

        hub_location = market_hubs.get(market, 60003761)

        for type_id in type_ids:
            type_prices = {'buy': None, 'sell': None}

            # Look up in price_data
            if market in price_data and type_id in price_data[market]:
                orders = price_data[market][type_id]

                buy_orders = [o for o in orders.values() if o['is_buy_order']]
                sell_orders = [o for o in orders.values() if not o['is_buy_order']]

                if buy_orders:
                    # Highest buy order
                    type_prices['buy'] = max(o['price'] for o in buy_orders)

                if sell_orders:
                    # Lowest sell order
                    type_prices['sell'] = min(o['price'] for o in sell_orders)

            prices[type_id] = type_prices

        return prices


# ============================================================================
# Global SDE Loader Initialization
# ============================================================================

sde_loader = None


def init_reprocessing_sde(sde_path: str):
    """Initialize the reprocessing SDE loader."""
    global sde_loader
    sde_loader = ReprocessingSDELoader(sde_path)
    return sde_loader


def main():
    """Main entry point - requires SDE path."""
    import argparse

    parser = argparse.ArgumentParser(description='Reprocessing API')
    parser.add_argument('--port', type=int, default=5000, help='Port to listen on')
    parser.add_argument('--address', type=str, default='127.0.0.1', help='Address to listen on')
    parser.add_argument('--sde', type=str, required=True, help='Path to SDE directory')

    args = parser.parse_args()

    if not os.path.isdir(args.sde):
        print(f"Error: SDE directory not found at {args.sde}")
        return 1

    # Initialize SDE
    print(f"Loading reprocessing SDE from {args.sde}...")
    init_reprocessing_sde(args.sde)

    # Register API resources
    api.add_resource(ReprocessConfig, '/v1/config')
    api.add_resource(ReprocessEndpoint, '/v1/reprocess')

    print("Reprocessing API endpoints initialized:")
    print("  POST /v1/config")
    print("  POST /v1/reprocess")

    from market_tools import app as flask_app
    flask_app.run(host=args.address, port=args.port, debug=False)

    return 0


if __name__ == '__main__':
    exit(main())
