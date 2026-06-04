#!/usr/bin/env python3
"""
Market Tools API - Industrialists' market data and price book management.
Includes reprocessing functionality.
"""

import gzip
import csv
import io
import os
import argparse
import json
import hashlib
import secrets
import base64
from datetime import datetime
from collections import defaultdict
from typing import Dict, List, Optional, Any, Tuple
from flask import Flask, request, jsonify
from flask_restful import Api, Resource
import yaml

app = Flask(__name__)
api = Api(app)


# ============================================================================
# Reprocessing Data Structures (moved from reprocessing.py for integration)
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
        self.type_materials = {}
        self.category_map = {}
        self.group_map = {}
        self.item_ore_processing_skill = {}
        self.ship_volumes = {}
        self.type_metadata = {}
        self.load_sde()

    def load_sde(self):
        """Load all SDE data from compressed CSV files."""
        # Load invTypes
        types_file = os.path.join(self.sde_path, "invTypes.csv.bz2")
        if os.path.exists(types_file):
            self._load_types(types_file)

        # Load invTypeMaterials
        materials_file = os.path.join(self.sde_path, "invTypeMaterials.csv.bz2")
        if os.path.exists(materials_file):
            self._load_materials(materials_file)

        # Load invCategories
        categories_file = os.path.join(self.sde_path, "invCategories.csv.bz2")
        if os.path.exists(categories_file):
            self._load_categories(categories_file)

        # Load invGroups
        groups_file = os.path.join(self.sde_path, "invGroups.csv.bz2")
        if os.path.exists(groups_file):
            self._load_groups(groups_file)

        # Load dgmTypeAttributes for ore/moon/gas processing skill reference
        attributes_file = os.path.join(self.sde_path, "dgmTypeAttributes.csv.bz2")
        if os.path.exists(attributes_file):
            self._load_attributes(attributes_file)

        # Load ship volumes
        ship_volumes_file = os.path.join(self.sde_path, "ship_volumes.yaml")
        if os.path.exists(ship_volumes_file):
            self._load_ship_volumes(ship_volumes_file)

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
                        volume_str = str(volume_str).strip()
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
        type_name = self.get_type_name(type_id)
        if type_name in self.ship_volumes:
            return self.ship_volumes[type_name] * quantity

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

        if cat_name == 'asteroid':
            return True
        if group_name == 'ice':
            return True
        if cat_name == 'ice product':
            return True

        return False

    def is_gas(self, type_id: int) -> bool:
        """Check if item is gas (compressed variants included)."""
        if type_id in self.GAS_TYPES:
            return True
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


# Reprocessing API key storage
API_KEYS: Dict[str, Dict] = {}


def generate_api_key(config: Dict) -> str:
    """Generate a unique API key from config."""
    config_str = json.dumps(config, sort_keys=True)
    hash_input = config_str + secrets.token_hex(8)
    return hashlib.sha256(hash_input.encode()).hexdigest()[:32]


def parse_api_key(key: str) -> Optional[Dict]:
    """Parse API key to get configuration."""
    return API_KEYS.get(key)


def validate_structure_config(structure: Optional[Dict]) -> Tuple[bool, Optional[Dict], Optional[str]]:
    """Validate structure configuration. Returns (is_valid, result_dict, error_message)."""
    if not structure:
        return True, None, None

    valid_types = ["npc_station", "citadel", "athanor", "tatara"]
    valid_rigs = [None, "t1", "t2"]
    valid_security = ["highsec", "lowsec", "nullsec"]

    errors = []
    result = {}

    stype = structure.get('type', 'npc_station')
    if stype not in valid_types:
        return False, None, f"Invalid structure type: {stype}"
    result['type'] = stype

    rig = structure.get('rig', None)
    if stype == "npc_station" and rig is not None:
        return False, None, "npc_station cannot specify a rig"
    if rig is not None and rig not in valid_rigs:
        return False, None, f"Invalid rig value: {rig}"
    result['rig'] = rig

    security = structure.get('security', 'highsec')
    if security not in valid_security:
        return False, None, f"Invalid security value: {security}"
    result['security'] = security

    return True, result, None


def validate_skills(skills: Optional[Dict]) -> Tuple[bool, Optional[Dict], Optional[str]]:
    """Validate skills configuration."""
    if not skills:
        return True, None, None

    result = {}
    for skill, level in skills.items():
        if not isinstance(level, int) or level < 0 or level > 5:
            return False, None, f"Skill level must be integer 0-5, got: {level}"
        result[skill] = level

    return True, result, None


def validate_implant(implant: Optional[str]) -> Tuple[bool, Optional[str]]:
    """Validate implant configuration."""
    if not implant:
        return True, None

    valid_implants = [None, "RX-801", "RX-802", "RX-804"]
    if implant not in valid_implants:
        return False, None

    return True, implant


def calculate_yield_config(structure: Optional[Dict], skills: Optional[Dict], implant: Optional[str]) -> Dict:
    """Calculate yield percentages from configuration."""
    # Default yields
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

    # Get structure type (default npc_station)
    stype = structure.get('type', 'npc_station') if structure else 'npc_station'

    # Get rig (default null, npc_station can't have rig)
    rig = structure.get('rig', None) if structure and stype != 'npc_station' else None

    # Get security (default highsec)
    security = structure.get('security', 'highsec') if structure else 'highsec'

    # Get skill levels
    reprocess_skill = (skills or {}).get("Reprocessing", 0)
    reprocess_eff_skill = (skills or {}).get("Reprocessing Efficiency", 0)
    op_skill = (skills or {}).get("Ore Processing", 0)

    # Get modifiers
    sm = sm_map.get(stype, 0.00)
    rm = rig_map.get(rig, 0)
    sec = sec_map.get(security, 0.00)
    im = implant_map.get(implant, 0.00)

    # Calculate ore yield using formula:
    # Yield = (50 + Rm) * (1 + Sec) * (1 + Sm) * (1 + R*0.03) * (1 + Re*0.02) * (1 + Op*0.02) * (1 + Im)
    ore_yield = (50 + rm) * (1 + sec) * (1 + sm) * \
                (1 + reprocess_skill * 0.03) * (1 + reprocess_eff_skill * 0.02) * \
                (1 + op_skill * 0.02) * (1 + im)

    # Gas uses same formula
    gas_yield = ore_yield

    # Scrap gets 50 + scrap reprocessing skill (max 55)
    scrap_yield = 50 + reprocess_skill
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
            Tuple of (products_dict, output_volume, reprocessed_materials)
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

        # Calculate total materials from all full portions (aggregated)
        total_materials = defaultdict(int)
        for mat in materials:
            mat_id = mat['materialTypeID']
            qty = mat['quantity'] * full_portions
            total_materials[mat_id] += qty

        # Apply efficiency and round down per resulting material
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

            # Process products to identify which can be further reprocessed
            for mat_id, mat_data in list(products.items()):
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
                    # Keep unchanged
                    drill_down_products[mat_id] = mat_data
                    drill_down_volume += mat_data['volume']

            products = drill_down_products
            output_volume = drill_down_volume
            reprocessed_list.extend(additional_reprocessed)

        return products, round(output_volume, 2), reprocessed_list


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
        is_valid, structure_result, error_msg = validate_structure_config(structure)
        if not is_valid:
            return {
                "code": "INVALID_VALUE",
                "path": "structure" + (".rig" if "rig" in str(error_msg).lower() else ""),
                "message": error_msg
            }, 404

        # Validate skills
        if skills:
            is_valid, skills_result, error_msg = validate_skills(skills)
            if not is_valid:
                return {
                    "code": "INVALID_VALUE",
                    "path": "skills",
                    "message": error_msg
                }, 404

        # Validate implant
        if implant:
            is_valid, implant_result = validate_implant(implant)
            if not is_valid:
                return {
                    "code": "INVALID_VALUE",
                    "path": "implant",
                    "message": f"Invalid implant: {implant}"
                }, 404

        # Build config and generate key
        config = {
            'structure': structure_result if structure_result else {},
            'skills': skills or {},
            'implant': implant
        }

        # Calculate yields for this config
        yields = calculate_yield_config(
            config['structure'],
            config['skills'],
            config['implant']
        )
        config['yields'] = yields

        # Generate API key
        api_key = generate_api_key(config)
        API_KEYS[api_key] = config

        return {"key": api_key}, 201


class ReprocessEndpoint(Resource):
    """Handle reprocessing requests."""
    sde = None  # Will be set in main()

    def get_market_prices(self, type_ids: List[int], market: str) -> Dict:
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

            # Look up in price_data (from market_tools module)
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

    def post(self):
        """Process reprocessing request."""
        local_sde = ReprocessEndpoint.sde
        if local_sde is None:
            return {"error": "INTERNAL_ERROR", "details": "SDE not initialized"}, 500

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
            type_id = local_sde.get_type_id(item_name)
            if type_id is None:
                unknown_items.append(item_name)
                continue

            if not isinstance(quantity, int) or quantity <= 0:
                return {
                    "error": "INVALID_FORMAT",
                    "details": f"Quantity must be positive integer, got: {quantity}"
                }, 400

            item_quantities[type_id] = quantity
            total_input_volume += local_sde.get_volume(type_id, quantity)

        # Return 404 for unknown items (sorted list)
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
        engine = ReprocessingEngine(local_sde)

        # Track output volumes
        total_output_volume = 0.0
        products_output = {}
        tracked_yields = {'ore': None, 'gas': None, 'scrap': None}

        # Process each item
        for type_id, quantity in item_quantities.items():
            # Determine which yield to use
            if local_sde.is_ore_or_ice(type_id):
                yield_pct = yields['ore']
                tracked_yields['ore'] = yields['ore']
            elif local_sde.is_gas(type_id):
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
                        (local_sde.get_volume(prod_id, 1) or 0.0), 2
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
            # Get prices for products and input items
            all_type_ids = list(item_quantities.keys()) + list(products_output.keys())
            market_prices = self.get_market_prices(all_type_ids, market)

            inputs_buy = 0.0
            inputs_sell = 0.0
            outputs_buy = 0.0
            outputs_sell = 0.0

            # Calculate input values
            for type_id, quantity in item_quantities.items():
                prices = market_prices.get(type_id, {})
                if prices.get('buy'):
                    inputs_buy += prices['buy'] * quantity
                if prices.get('sell'):
                    inputs_sell += prices['sell'] * quantity

            # Calculate output values
            for prod_id, prod_data in products_output.items():
                prices = market_prices.get(prod_id, {})
                if prices.get('buy'):
                    outputs_buy += prices['buy'] * prod_data['quantity']
                if prices.get('sell'):
                    outputs_sell += prices['sell'] * prod_data['quantity']

                result['products'][local_sde.get_type_name(prod_id)] = {
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
            # No market data - product names without pricing
            for prod_id, prod_data in products_output.items():
                result['products'][local_sde.get_type_name(prod_id)] = {
                    'quantity': prod_data['quantity'],
                    'volume': prod_data['volume'],
                    'buy': None,
                    'sell': None
                }

        return result, 201


# Global SDE loader
sde_loader = None

# Data storage - will be initialized with SDE
price_data: Dict[str, Dict[int, Dict[int, Dict]]] = defaultdict(lambda: defaultdict(dict))  # market -> type_id -> order_id -> order
stations: Dict[int, Dict] = {}  # location_id -> station info
name_to_id: Dict[str, int] = {}
id_to_name: Dict[int, str] = {}

# Main hub stations (by location_id)
MAIN_HUBS = {
    "jita": 60003761,   # Jita IV - Moon 4 - Caldari Navy Assembly Plant
    "amarr": 60008494,  # Amarr VIII (Oris) - Emperor Family Academy
    "dodixie": 60014718, # Dodixie IX - Moon 20 - Federation Navy Assembly Plant
    "rens": 60004588,   # Rens VI - Moon 8 - Brutor Tribe Treasury
    "hek": 60005614,    # Hek VIII - Moon 12 - Boundless Creation Factory
}

# Region mappings (simplified - would come from SDE)
REGIONS = {
    10000002: "The Forge",     # Jita region
    10000043: "Domain",        # Amarr region
    10000032: "Sinq Laison",   # Dodixie region
    10000030: "Verge Vendor",  # Rens region
    10000031: "Essence",       # Hek region
}


class SDELoader:
    """Handles loading and querying the Static Data Export."""

    def __init__(self, sde_path: str):
        self.sde_path = sde_path
        self.load_sde()

    def load_sde(self):
        """Load all SDE data from compressed CSV files."""
        # Load invTypes for name/ID resolution
        types_file = os.path.join(self.sde_path, "invTypes.csv.bz2")
        if os.path.exists(types_file):
            self._load_types(types_file)

        # Load stations
        stations_file = os.path.join(self.sde_path, "staStations.csv.bz2")
        if os.path.exists(stations_file):
            self._load_stations(stations_file)

    def _load_types(self, filepath: str):
        """Load type names and IDs from invTypes.csv."""
        try:
            import bz2
            with bz2.open(filepath, 'rt', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    type_id = int(row['typeID'])
                    name = row.get('typeName', '')
                    if name:
                        id_to_name[type_id] = name
                        name_to_id[name] = type_id
        except Exception as e:
            raise ValueError(f"Error loading types file: {e}")

    def _load_stations(self, filepath: str):
        """Load station data from staStations.csv."""
        try:
            import bz2
            with bz2.open(filepath, 'rt', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    location_id = int(row['stationID'])
                    if location_id not in stations:
                        stations[location_id] = {
                            'location_id': location_id,
                            'type': 'Station',
                            'name': row.get('stationName', ''),
                            'system_id': int(row.get('solarSystemID', 0)),
                            'region_id': int(row.get('regionID', 0))
                        }
        except Exception as e:
            raise ValueError(f"Error loading stations file: {e}")

    def get_type_name(self, type_id: int) -> Optional[str]:
        """Get type name from ID."""
        return id_to_name.get(type_id)

    def get_type_id(self, name: str) -> Optional[int]:
        """Get type ID from name."""
        return name_to_id.get(name)

    def get_region_name(self, region_id: int) -> str:
        """Get region name from ID."""
        return REGIONS.get(region_id, f"Unknown Region {region_id}")


class PriceIngestion(Resource):
    """Handle price data ingestion from gzipped CSV."""

    def post(self):
        try:
            # Get raw gzipped data
            if not request.data:
                return {"error": "INVALID_FORMAT", "details": "No data provided"}, 400

            # Decompress gzipped data
            try:
                decompressed = gzip.decompress(request.data)
            except Exception as e:
                return {"error": "INVALID_FORMAT", "details": f"Decompression error: {e}"}, 400

            # Parse CSV
            try:
                csv_data = csv.DictReader(io.StringIO(decompressed.decode('utf-8')))
            except Exception as e:
                return {"error": "INVALID_FORMAT", "details": f"CSV parsing error: {e}"}, 400

            # Validate required columns
            required_columns = [
                'order_id', 'duration', 'is_buy_order', 'issued', 'location_id',
                'min_volume', 'price', 'range', 'system_id', 'type_id',
                'volume_remain', 'volume_total'
            ]
            if not csv_data.fieldnames:
                return {"error": "INVALID_FORMAT", "details": "Empty CSV file"}, 400

            missing = [col for col in required_columns if col not in csv_data.fieldnames]
            if missing:
                return {"error": "INVALID_FORMAT", "details": f"Missing required columns: {', '.join(missing)}"}, 400

            # Get query parameters
            market = request.args.get('market', 'jita')
            mode = request.args.get('mode', 'replace')
            location_id_override = request.args.get('location_id')
            if location_id_override:
                try:
                    location_id_override = int(location_id_override)
                except ValueError:
                    return {"error": "INVALID_FORMAT", "details": "Invalid location_id"}, 400

            # Process orders - group by type_id
            new_orders_by_type = defaultdict(dict)

            for row in csv_data:
                try:
                    order = {
                        'order_id': int(row['order_id']),
                        'duration': int(row['duration']),
                        'is_buy_order': row['is_buy_order'].lower() in ('true', '1', 'yes'),
                        'issued': row['issued'],
                        'location_id': int(row['location_id']),
                        'min_volume': int(row['min_volume']),
                        'price': float(row['price']),
                        'range': row['range'],
                        'system_id': int(row['system_id']),
                        'type_id': int(row['type_id']),
                        'volume_remain': int(row['volume_remain']),
                        'volume_total': int(row['volume_total']),
                    }

                    type_id = order['type_id']

                    # Apply location filter if specified
                    if location_id_override and order['location_id'] != location_id_override:
                        continue

                    # For append mode, handle upsert by order_id within each type
                    if mode == 'append':
                        existing = new_orders_by_type[type_id].get(order['order_id'])
                        if existing is None or order['issued'] > existing['issued']:
                            new_orders_by_type[type_id][order['order_id']] = order
                    else:
                        new_orders_by_type[type_id][order['order_id']] = order

                except (ValueError, KeyError) as e:
                    return {"error": "INVALID_FORMAT", "details": f"Invalid data in row: {e}"}, 400

            rows_processed = sum(len(orders) for orders in new_orders_by_type.values())

            # Store in price_data
            if mode == 'replace':
                # Replace mode - clear existing data
                price_data[market] = {}

            # Merge or replace data
            existing_market = price_data[market]
            for type_id, orders_by_id in new_orders_by_type.items():
                if mode == 'append':
                    # Merge with existing orders
                    existing_type_orders = existing_market.get(type_id, {})
                    merged = {**existing_type_orders, **orders_by_id}
                    # Handle upsert conflicts where newer issued wins
                    for order_id, new_order in orders_by_id.items():
                        if order_id in existing_type_orders:
                            existing_order = existing_type_orders[order_id]
                            if existing_order['issued'] > new_order['issued']:
                                merged[order_id] = existing_order
                    existing_market[type_id] = merged
                else:
                    existing_market[type_id] = orders_by_id

            return {"status": "PRICES_UPDATED", "count": rows_processed}, 200

        except Exception as e:
            return {"error": "INVALID_FORMAT", "details": str(e)}, 400


class StationIngestion(Resource):
    """Handle station data ingestion from gzipped CSV."""

    def post(self):
        try:
            if not request.data:
                return {"error": "INVALID_FORMAT", "details": "No data provided"}, 400

            try:
                decompressed = gzip.decompress(request.data)
            except Exception as e:
                return {"error": "INVALID_FORMAT", "details": f"Decompression error: {e}"}, 400

            try:
                csv_data = csv.DictReader(io.StringIO(decompressed.decode('utf-8')))
            except Exception as e:
                return {"error": "INVALID_FORMAT", "details": f"CSV parsing error: {e}"}, 400

            required_columns = ['location_id', 'type', 'name']
            if not csv_data.fieldnames:
                return {"error": "INVALID_FORMAT", "details": "Empty CSV file"}, 400

            missing = [col for col in required_columns if col not in csv_data.fieldnames]
            if missing:
                return {"error": "INVALID_FORMAT", "details": f"Missing required columns: {', '.join(missing)}"}, 400

            rows_processed = 0
            for row in csv_data:
                try:
                    location_id = int(row['location_id'])
                    station_type = row['type']
                    name = row['name']

                    station = {
                        'location_id': location_id,
                        'type': station_type,
                        'name': name,
                        'system_id': int(row.get('system_id', 0)),
                        'region_id': int(row.get('region_id', 0))
                    }

                    stations[location_id] = station
                    rows_processed += 1

                except (ValueError, KeyError) as e:
                    return {"error": "INVALID_FORMAT", "details": f"Invalid data in row: {e}"}, 400

            return {"status": "STATIONS_UPDATED", "count": rows_processed}, 200

        except Exception as e:
            return {"error": "INVALID_FORMAT", "details": str(e)}, 400


class MarketRegion(Resource):
    """Handle market statistics for a region."""

    def get(self, regionID):
        """Get market stats for a region."""
        type_ids_param = request.args.get('type_ids')
        hubs_param = request.args.get('hubs')

        type_ids = []
        if type_ids_param:
            type_ids = [int(tid.strip()) for tid in type_ids_param.split(',') if tid.strip()]

        include_hubs = hubs_param is not None

        # Find the hub with most sell orders
        hub_orders = defaultdict(lambda: {'sell': 0, 'buy': 0, 'orders': []})
        region_name = sde_loader.get_region_name(int(regionID))

        # Collect all orders in the region
        all_orders = []
        for market, types_dict in price_data.items():
            for type_id, order_dict in types_dict.items():
                all_orders.extend(order_dict.values())

        if not all_orders and not type_ids:
            return {
                "name": region_name,
                "sell_orders": 0,
                "buy_orders": 0,
                "sell_value": 0.0,
                "buy_value": 0.0,
                "unique_items": 0
            }, 200

        # Calculate stats
        sell_orders = 0
        buy_orders = 0
        sell_value = 0.0
        buy_value = 0.0
        unique_items = set()
        types_data = {}

        for type_id in type_ids:
            type_name = sde_loader.get_type_name(type_id) or f"Unknown {type_id}"
            orders = []

            # Collect orders for this type
            for market, orders_dict in price_data.items():
                type_orders = orders_dict.get(type_id, {})
                orders.extend(type_orders.values())

            buy_list = [o for o in orders if o['is_buy_order']]
            sell_list = [o for o in orders if not o['is_buy_order']]

            if buy_list or sell_list:
                unique_items.add(type_id)

            if buy_list:
                highest_buy = max(o['price'] for o in buy_list)
                outlier_threshold = highest_buy * 1.1
                filtered_buy = [o for o in buy_list if o['price'] >= outlier_threshold]
                if filtered_buy:
                    highest_buy = max(o['price'] for o in filtered_buy)
                else:
                    filtered_buy = buy_list

                buy_orders += len(filtered_buy)
                buy_value += sum(o['price'] * o['volume_remain'] for o in filtered_buy)

            if sell_list:
                lowest_sell = min(o['price'] for o in sell_list)
                outlier_threshold = lowest_sell * 10
                filtered_sell = [o for o in sell_list if o['price'] <= outlier_threshold]
                if filtered_sell:
                    lowest_sell = min(o['price'] for o in filtered_sell)
                else:
                    filtered_sell = sell_list

                sell_orders += len(filtered_sell)
                sell_value += sum(o['price'] * o['volume_remain'] for o in filtered_sell)

            if type_ids and (buy_list or sell_list):
                buy_price = None
                sell_price = None

                if filtered_buy:
                    buy_price = max(o['price'] for o in filtered_buy)
                if filtered_sell:
                    sell_price = min(o['price'] for o in filtered_sell)

                split = None
                if buy_price is not None and sell_price is not None:
                    split = (buy_price + sell_price) / 2

                types_data[type_name] = {
                    "buy": buy_price,
                    "sell": sell_price,
                    "split": split,
                    "buy_orders": len(filtered_buy),
                    "sell_orders": len(filtered_sell)
                }

        response = {
            "name": region_name,
            "sell_orders": sell_orders,
            "buy_orders": buy_orders,
            "sell_value": round(sell_value / 1_000_000_000, 2),
            "buy_value": round(buy_value / 1_000_000_000, 2),
            "unique_items": len(unique_items) if type_ids else 0
        }

        if type_ids:
            response['types'] = types_data

        if include_hubs:
            hub_list = self._get_hub_stats(int(regionID))
            response['hubs'] = hub_list

        return response, 200

    def _get_hub_stats(self, region_id: int):
        """Get hub statistics for a region."""
        hubs_data = []

        for hub_name, hub_location in MAIN_HUBS.items():
            orders = []
            for market, orders_dict in price_data.items():
                for type_id, order_dict in orders_dict.items():
                    for order_id, order in order_dict.items():
                        if order['location_id'] == hub_location:
                            orders.append(order)

            if not orders:
                continue

            sell_orders = [o for o in orders if not o['is_buy_order']]
            buy_orders = [o for o in orders if o['is_buy_order']]

            sell_value = sum(o['price'] * o['volume_remain'] for o in sell_orders)
            sell_value_billions = round(sell_value / 1_000_000_000, 2)

            # Get station name
            station_name = stations.get(hub_location, {}).get('name', str(hub_location))

            hubs_data.append({
                "station": station_name,
                "orders": len(sell_orders),
                "sell_value": sell_value_billions
            })

        # Sort by number of sell orders
        hubs_data.sort(key=lambda x: x['orders'], reverse=True)
        return hubs_data


class MarketType(Resource):
    """Handle market statistics for a specific type in a region."""

    def get(self, regionID, typeID):
        """Get detailed market stats for a specific type in a region."""
        type_id = int(typeID)
        # Collect orders for this type
        orders = []
        for market, orders_dict in price_data.items():
            type_orders = orders_dict.get(type_id, {})
            orders.extend(type_orders.values())

        if not orders:
            type_name = sde_loader.get_type_name(int(typeID)) or f"Unknown {typeID}"
            return {
                "name": type_name,
                "buy": None,
                "sell": None,
                "buy_5pct": None,
                "sell_5pct": None,
                "buy_orders": 0,
                "sell_orders": 0,
                "buy_threshold": None,
                "sell_threshold": None,
                "buy_volume": 0,
                "sell_volume": 0,
                "total_value": 0.0
            }, 200

        buy_list = [o for o in orders if o['is_buy_order']]
        sell_list = [o for o in orders if not o['is_buy_order']]

        # Filter outliers
        buy_threshold = None
        sell_threshold = None

        if buy_list:
            highest_buy = max(o['price'] for o in buy_list)
            buy_threshold = highest_buy * 0.9  # 10% outlier
            buy_list = [o for o in buy_list if o['price'] >= buy_threshold]

        if sell_list:
            lowest_sell = min(o['price'] for o in sell_list)
            sell_threshold = lowest_sell * 10  # 10x outlier
            sell_list = [o for o in sell_list if o['price'] <= sell_threshold]

        # Calculate 5pct volume-weighted average
        buy_5pct = self._calculate_5pct_price(buy_list, reverse=True)  # Sort descending for buys
        sell_5pct = self._calculate_5pct_price(sell_list, reverse=False)  # Sort ascending for sells

        # Final prices
        buy_price = None
        sell_price = None
        if buy_list:
            buy_price = max(o['price'] for o in buy_list)
        if sell_list:
            sell_price = min(o['price'] for o in sell_list)

        type_name = sde_loader.get_type_name(int(typeID)) or f"Unknown {typeID}"

        # Total value using 5pct sell price
        total_value = 0.0
        if sell_5pct is not None:
            total_value = sum(o['volume_remain'] for o in sell_list) * sell_5pct

        return {
            "name": type_name,
            "buy": buy_price,
            "sell": sell_price,
            "buy_5pct": buy_5pct,
            "sell_5pct": sell_5pct,
            "buy_orders": len(buy_list),
            "sell_orders": len(sell_list),
            "buy_threshold": buy_threshold,
            "sell_threshold": sell_threshold,
            "buy_volume": sum(o['volume_remain'] for o in buy_list),
            "sell_volume": sum(o['volume_remain'] for o in sell_list),
            "total_value": round(total_value, 2)
        }, 200

    def _calculate_5pct_price(self, orders: List[Dict], reverse: bool) -> Optional[float]:
        """Calculate volume-weighted average price of top 5% of orders."""
        if not orders:
            return None

        # Sort by price
        sorted_orders = sorted(orders, key=lambda o: o['price'], reverse=reverse)

        total_volume = sum(o['volume_remain'] for o in sorted_orders)
        if total_volume == 0:
            return None

        target_volume = total_volume * 0.05
        accumulated = 0
        total_value = 0.0

        for order in sorted_orders:
            remaining = target_volume - accumulated
            if remaining <= 0:
                break

            take = min(order['volume_remain'], remaining)
            total_value += take * order['price']
            accumulated += take

        return round(total_value / accumulated, 2) if accumulated > 0 else None


class HubCompare(Resource):
    """Compare market statistics across main hubs."""

    def get(self, typeID):
        """Get market stats for a type across all main hubs."""
        type_id = int(typeID)
        result = {}
        has_data = False

        for hub_name, hub_location in MAIN_HUBS.items():
            orders = []
            for market, orders_dict in price_data.items():
                type_orders = orders_dict.get(type_id, {})
                for order_id, order in type_orders.items():
                    if order['location_id'] == hub_location:
                        orders.append(order)

            if not orders:
                continue

            has_data = True
            buy_list = [o for o in orders if o['is_buy_order']]
            sell_list = [o for o in orders if not o['is_buy_order']]

            # Filter outliers
            if buy_list:
                highest_buy = max(o['price'] for o in buy_list)
                buy_threshold = highest_buy * 0.9
                buy_list = [o for o in buy_list if o['price'] >= buy_threshold]

            if sell_list:
                lowest_sell = min(o['price'] for o in sell_list)
                sell_threshold = lowest_sell * 10
                sell_list = [o for o in sell_list if o['price'] <= sell_threshold]

            # Calculate prices
            buy_price = max(o['price'] for o in buy_list) if buy_list else None
            sell_price = min(o['price'] for o in sell_list) if sell_list else None

            buy_volume = sum(o['volume_remain'] for o in buy_list) if buy_list else 0
            sell_volume = sum(o['volume_remain'] for o in sell_list) if sell_list else 0

            # Calculate 5pct prices
            buy_5pct = self._calculate_5pct(buy_list, reverse=True) if buy_list else None
            sell_5pct = self._calculate_5pct(sell_list, reverse=False) if sell_list else None

            # Calculate value (5pct sell * sell volume in billions)
            value = 0.0
            if sell_5pct and sell_volume > 0:
                value = round((sell_5pct * sell_volume) / 1_000_000_000, 2)

            result[f"{hub_name}_buy"] = buy_price
            result[f"{hub_name}_sell"] = sell_price
            result[f"{hub_name}_sell_volume"] = float(sell_volume)
            result[f"{hub_name}_buy_volume"] = float(buy_volume)
            result[f"{hub_name}_value"] = value if value > 0 else 0.0
            result[f"{hub_name}_sell_5pct"] = sell_5pct
            result[f"{hub_name}_buy_5pct"] = buy_5pct

        if not has_data:
            return {"error": "NO_PRICE_DATA", "details": f"No price data found for type {typeID} in any hub"}, 404

        return result, 200

    def _calculate_5pct(self, orders: List[Dict], reverse: bool) -> Optional[float]:
        """Calculate volume-weighted average price of top 5% of orders."""
        if not orders:
            return None

        sorted_orders = sorted(orders, key=lambda o: o['price'], reverse=reverse)
        total_volume = sum(o['volume_remain'] for o in sorted_orders)

        if total_volume == 0:
            return None

        target_volume = total_volume * 0.05
        accumulated = 0
        total_value = 0.0

        for order in sorted_orders:
            remaining = target_volume - accumulated
            if remaining <= 0:
                break
            take = min(order['volume_remain'], remaining)
            total_value += take * order['price']
            accumulated += take

        return round(total_value / accumulated, 2) if accumulated > 0 else None


class HealthCheck(Resource):
    """Simple health check endpoint."""

    def get(self):
        return {"status": "healthy"}, 200


# Global SDE loaders
sde_loader = None
reprocessing_sde_loader = None


def main():
    global sde_loader, reprocessing_sde_loader

    parser = argparse.ArgumentParser(description='Market Tools API')
    parser.add_argument('--port', type=int, default=5000, help='Port to listen on')
    parser.add_argument('--address', type=str, default='127.0.0.1', help='Address to listen on')
    parser.add_argument('--sde', type=str, required=True, help='Path to SDE directory')

    args = parser.parse_args()

    # Verify SDE path
    if not os.path.isdir(args.sde):
        print(f"Error: SDE directory not found at {args.sde}")
        return 1

    # Load SDE
    print(f"Loading SDE from {args.sde}...")
    sde_loader = SDELoader(args.sde)
    print(f"SDE loaded: {len(id_to_name)} type names, {len(stations)} stations")

    # Initialize reprocessing SDE loader
    print(f"Loading reprocessing SDE data...")
    reprocessing_sde_loader = ReprocessingSDELoader(args.sde)

    # Register API resources
    api.add_resource(PriceIngestion, '/v1/prices')
    api.add_resource(StationIngestion, '/v1/stations')
    api.add_resource(MarketRegion, '/v1/market/<regionID>')
    api.add_resource(MarketType, '/v1/market/<regionID>/<typeID>')
    api.add_resource(HubCompare, '/v1/hub-compare/<typeID>')
    api.add_resource(HealthCheck, '/health')
    api.add_resource(ReprocessConfig, '/v1/config')

    # Pass the reprocessing loader to the ReprocessEndpoint
    ReprocessEndpoint.sde = reprocessing_sde_loader
    api.add_resource(ReprocessEndpoint, '/v1/reprocess')

    print("API endpoints registered:")
    print("  GET  /health")
    print("  POST /v1/prices")
    print("  POST /v1/stations")
    print("  GET  /v1/market/<regionID>")
    print("  GET  /v1/market/<regionID>/<typeID>")
    print("  GET  /v1/hub-compare/<typeID>")
    print("  POST /v1/config")
    print("  POST /v1/reprocess")

    # Run Flask app
    app.run(host=args.address, port=args.port, debug=False)
    return 0


if __name__ == '__main__':
    exit(main())
