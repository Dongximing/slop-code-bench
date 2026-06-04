#!/usr/bin/env python3
"""
Reprocessing module - handles reprocessing calculations, yield formulas,
API key generation, and drill-down reprocessing.
"""

import csv
import bz2
import secrets
import hashlib
import hmac
import json
import time
from pathlib import Path
from collections import defaultdict
from typing import Optional, Dict, Any


class ReprocessingConfig:
    """Configuration constants for reprocessing calculations."""

    # Structure modifiers
    STRUCTURE_MODIFIERS = {
        "npc_station": 0.00,
        "citadel": 0.00,
        "athanor": 0.02,
        "tatara": 0.055
    }

    # Rig modifiers
    RIG_MODIFIERS = {
        None: 0,
        "t1": 1,
        "t2": 3
    }

    # Security modifiers
    SECURITY_MODIFIERS = {
        "highsec": 0.00,
        "lowsec": 0.06,
        "nullsec": 0.12
    }

    # Implant modifiers
    IMPLANT_MODIFIERS = {
        None: 0.00,
        "RX-801": 0.01,
        "RX-802": 0.02,
        "RX-804": 0.04
    }

    # Category and group IDs
    CATEGORY_ORE = 25  # Asteroid
    GROUP_ICE = 465
    GROUP_GAS_ISOTOPES = 422

    # Scrap reprocessing constants
    SCRAP_REPROC_BASE = 50.0
    SCRAP_REPROC_SKILL_BONUS = 3.0
    MAX_TOTAL_SCRAP_REPROC = 55.0


class Reprocessor:
    """
    Handles reprocessing calculations using SDE data.
    """

    def __init__(self, sde_path: str):
        """Initialize reprocessor with path to SDE directory."""
        self.sde_path = Path(sde_path)

        # Type mappings
        self.type_id_to_name: dict[int, str] = {}
        self.type_name_to_id: dict[str, int] = {}
        self.type_id_to_volume: dict[int, float] = {}
        self.type_id_to_mass: dict[int, float] = {}
        self.type_id_to_portionsize: dict[int, int] = {}
        self.type_id_to_group: dict[int, int] = {}
        self.type_id_to_materials: dict[int, list] = {}

        # Market data: market_name -> type_id -> price data
        self.market_prices: dict[str, dict[int, dict]] = defaultdict(lambda: defaultdict(dict))

        self._load_all_sde()

    def _load_all_sde(self):
        """Load all SDE data files needed for reprocessing."""
        self._load_inv_types()
        self._load_inv_groups()
        self._load_inv_type_materials()

    def _load_inv_types(self):
        """Load item types from invTypes.csv."""
        filepath = self.sde_path / "invTypes.csv.bz2"
        if not filepath.exists():
            raise FileNotFoundError(f"SDE file not found: {filepath}")

        with bz2.open(filepath, 'rt', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    type_id = int(row['typeID'])
                    type_name = row['typeName']
                    volume = float(row['volume']) if row['volume'] and row['volume'] != '0E-10' else 0.0
                    mass = float(row['mass']) if row['mass'] else 0.0
                    portion_size = int(row['portionSize']) if row['portionSize'] else 0
                    group_id = int(row['groupID']) if row['groupID'] else 0

                    self.type_id_to_name[type_id] = type_name
                    self.type_name_to_id[type_name] = type_id
                    self.type_id_to_volume[type_id] = volume
                    self.type_id_to_mass[type_id] = mass
                    self.type_id_to_portionsize[type_id] = portion_size
                    self.type_id_to_group[type_id] = group_id
                except (KeyError, ValueError):
                    continue

    def _load_inv_groups(self):
        """Load item groups from invGroups.csv."""
        filepath = self.sde_path / "invGroups.csv.bz2"
        if not filepath.exists():
            return

        with bz2.open(filepath, 'rt', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    group_id = int(row['groupID'])
                    category_id = int(row['categoryID'])
                    # Already stored per type, but we need group->category mapping
                    self.type_id_to_group[group_id] = category_id
                except (KeyError, ValueError):
                    continue

    def _load_inv_type_materials(self):
        """Load material relationships from invTypeMaterials.csv."""
        filepath = self.sde_path / "invTypeMaterials.csv.bz2"
        if not filepath.exists():
            return

        with bz2.open(filepath, 'rt', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    type_id = int(row['typeID'])
                    material_type_id = int(row['materialTypeID'])
                    quantity = int(row['quantity'])

                    if type_id not in self.type_id_to_materials:
                        self.type_id_to_materials[type_id] = []
                    self.type_id_to_materials[type_id].append({
                        'type_id': material_type_id,
                        'quantity': quantity
                    })
                except (KeyError, ValueError):
                    continue

    def load_market_data(self, market_name: str, price_data: dict):
        """Load market price data for a hub."""
        self.market_prices[market_name] = price_data

    def get_type_id(self, name: str) -> Optional[int]:
        """Get type ID from item name."""
        return self.type_name_to_id.get(name)

    def get_type_name(self, type_id: int) -> Optional[str]:
        """Get type name from ID."""
        return self.type_id_to_name.get(type_id)

    def get_volume(self, type_id: int) -> float:
        """Get item volume."""
        return self.type_id_to_volume.get(type_id, 0.0)

    def get_portionsize(self, type_id: int) -> int:
        """Get item portion size for reprocessing."""
        return self.type_id_to_portionsize.get(type_id, 0)

    def get_materials(self, type_id: int) -> list:
        """Get materials for an item."""
        return self.type_id_to_materials.get(type_id, [])

    def is_ore_or_ice(self, type_id: int) -> bool:
        """Check if item is ore or ice."""
        group_id = self.type_id_to_group.get(type_id, 0)
        return group_id == ReprocessingConfig.CATEGORY_ORE or group_id == ReprocessingConfig.GROUP_ICE

    def is_gas(self, type_id: int) -> bool:
        """Check if item is gas isotopes."""
        group_id = self.type_id_to_group.get(type_id, 0)
        return group_id == ReprocessingConfig.GROUP_GAS_ISOTOPES


class APIKeyManager:
    """Manages API keys for configuration-driven yields."""

    def __init__(self):
        self.secret = secrets.token_hex(32)
        self.keys: dict[str, dict] = {}

    def create_key(self, config: dict) -> str:
        """Create a new API key encoding the configuration."""
        key_id = secrets.token_urlsafe(32)
        full_config = self._apply_defaults(config)
        self._validate_config(full_config)

        timestamp = time.time()
        expires = timestamp + (30 * 24 * 60 * 60)

        config_str = json.dumps(full_config, sort_keys=True)
        signature = hmac.new(
            self.secret.encode(),
            config_str.encode(),
            hashlib.sha256
        ).hexdigest()

        self.keys[key_id] = {
            'config': full_config,
            'created': timestamp,
            'expires': expires,
            'signature': signature
        }

        return f"{key_id}.{signature[:32]}"

    def _apply_defaults(self, config: dict) -> dict:
        """Apply default values for omitted fields."""
        result = {
            'structure': {
                'type': 'npc_station',
                'rig': None,
                'security': 'highsec'
            },
            'skills': {},
            'implant': None
        }

        if 'structure' in config:
            result['structure'].update(config['structure'])

        # Validate structure type
        valid_types = ['npc_station', 'citadel', 'athanor', 'tatara']
        if result['structure']['type'] not in valid_types:
            raise ValueError(f"Invalid structure type: {result['structure']['type']}")

        # npc_station cannot have rig
        if result['structure']['type'] == 'npc_station' and result['structure'].get('rig') is not None:
            raise ValueError("npc_station cannot specify a rig")

        # Validate security
        valid_security = ['highsec', 'lowsec', 'nullsec']
        if result['structure']['security'] not in valid_security:
            raise ValueError(f"Invalid security: {result['structure']['security']}")

        # Merge skills
        if 'skills' in config:
            result['skills'] = config['skills']

        # Validate skill levels
        for skill, level in result['skills'].items():
            if not isinstance(level, int) or level < 0 or level > 5:
                raise ValueError(f"Invalid skill level for {skill}: {level}")

        # Validate implant
        valid_implants = [None, 'RX-801', 'RX-802', 'RX-804']
        if 'implant' in config:
            if config['implant'] not in valid_implants:
                raise ValueError(f"Invalid implant: {config['implant']}")
            result['implant'] = config['implant']

        return result

    def _validate_config(self, config: dict) -> dict:
        """Validate configuration."""
        return config

    def validate_key(self, key_str: str) -> dict:
        """Validate an API key and return the configuration."""
        if not key_str or '.' not in key_str:
            raise ValueError("Invalid key format")

        key_id, signature = key_str.split('.', 1)

        if key_id not in self.keys:
            raise ValueError("Unknown key")

        key_data = self.keys[key_id]

        if time.time() > key_data['expires']:
            raise ValueError("Key expired")

        config_str = json.dumps(key_data['config'], sort_keys=True)
        expected_sig = hmac.new(
            self.secret.encode(),
            config_str.encode(),
            hashlib.sha256
        ).hexdigest()[:32]

        if not hmac.compare_digest(signature, expected_sig):
            raise ValueError("Invalid signature")

        return key_data['config']

    def get_invalid_fields(self, config: dict) -> Optional[Dict[str, str]]:
        """Validate config and return details about invalid fields."""
        if 'structure' in config:
            st = config['structure']
            if 'type' in st:
                valid_types = ['npc_station', 'citadel', 'athanor', 'tatara']
                if st['type'] not in valid_types:
                    return {
                        'path': 'structure.type',
                        'message': f"Invalid structure type '{st['type']}'. Must be npc_station, citadel, athanor, or tatara"
                    }

            if 'rig' in st:
                valid_rigs = [None, 't1', 't2']
                if st['rig'] not in valid_rigs:
                    return {
                        'path': 'structure.rig',
                        'message': f"Invalid rig '{st['rig']}'. Must be null, t1, or t2"
                    }
                if st.get('type', 'npc_station') == 'npc_station' and st['rig'] is not None:
                    return {
                        'path': 'structure.rig',
                        'message': "npc_station cannot specify a rig"
                    }

            if 'security' in st:
                valid_sec = ['highsec', 'lowsec', 'nullsec']
                if st['security'] not in valid_sec:
                    return {
                        'path': 'structure.security',
                        'message': f"Invalid security '{st['security']}'. Must be highsec, lowsec, or nullsec"
                    }

        if 'skills' in config:
            for skill, level in config['skills'].items():
                if not isinstance(level, int) or level < 0 or level > 5:
                    return {
                        'path': f'skills.{skill}',
                        'message': f"Invalid skill level {level}. Must be an integer 0-5"
                    }

        if 'implant' in config:
            valid_implants = [None, 'RX-801', 'RX-802', 'RX-804']
            if config['implant'] not in valid_implants:
                return {
                    'path': 'implant',
                    'message': f"Invalid implant '{config['implant']}'. Must be null, RX-801, RX-802, or RX-804"
                }

        return None


class ReprocessingEngine:
    """
    Core reprocessing engine that handles reprocessing calculations,
    drill-down, and output generation.
    """

    def __init__(self, reprocessor: Reprocessor, api_manager: APIKeyManager):
        self.reprocessor = reprocessor
        self.api_manager = api_manager

    def calculate_yield(self, reproc_type: str, config: dict, skills: dict = None) -> float:
        """
        Calculate reprocessing yield percentage.
        Ore/Ice/Gas:
          Yield = (50 + Rm) * (1 + Sec) * (1 + Sm) * (1 + (R * 0.03)) * (1 + (Re * 0.02)) * (1 + (Op * 0.02)) * (1 + Im)

        Scrap: 50 + scrap reprocessing skill (max 55)
        """
        if skills is None:
            skills = {}

        structure_config = config.get('structure', {})
        rig = structure_config.get('rig')
        security = structure_config.get('security', 'highsec')
        implant = config.get('implant')

        structure_type = structure_config.get('type', 'npc_station')

        Sm = ReprocessingConfig.STRUCTURE_MODIFIERS.get(structure_type, 0.00)
        Rm = ReprocessingConfig.RIG_MODIFIERS.get(rig, 0)
        Sec = ReprocessingConfig.SECURITY_MODIFIERS.get(security, 0.00)
        Im = ReprocessingConfig.IMPLANT_MODIFIERS.get(implant, 0.00)

        if reproc_type == 'scrap':
            R = skills.get('Reprocessing', 0)
            base_yield = ReprocessingConfig.SCRAP_REPROC_BASE + (R * ReprocessingConfig.SCRAP_REPROC_SKILL_BONUS)
            return min(base_yield, ReprocessingConfig.MAX_TOTAL_SCRAP_REPROC)

        R = skills.get('Reprocessing', 0)
        Re = skills.get('Reprocessing Efficiency', 0)

        Op = 0
        for skill_name, level in skills.items():
            if 'ore processing' in skill_name.lower() or 'gas' in skill_name.lower() or 'moon' in skill_name.lower():
                Op = max(Op, level)

        yield_pct = (50 + Rm) * (1 + Sec) * (1 + Sm) * (1 + (R * 0.03)) * (1 + (Re * 0.02)) * (1 + (Op * 0.02)) * (1 + Im)
        return round(yield_pct, 2)

    def get_efficiency_multiplier(self, api_config: dict = None) -> dict:
        """Get efficiency multipliers from API config or defaults."""
        if api_config:
            skills = api_config.get('skills', {})
            return {
                'ore': self.calculate_yield('ore', api_config, skills) / 100,
                'gas': self.calculate_yield('gas', api_config, skills) / 100,
                'scrap': self.calculate_yield('scrap', api_config, skills) / 100
            }

        return {
            'ore': 0.9063,
            'gas': 0.95,
            'scrap': 0.55
        }

    def get_reprocess_type(self, type_id: int) -> str:
        """Determine the reprocessing type for an item."""
        if self.reprocessor.is_ore_or_ice(type_id):
            return 'ore'
        elif self.reprocessor.is_gas(type_id):
            return 'gas'
        else:
            return 'scrap'

    def process_items(self, items: dict, efficiencies: dict = None,
                      api_key: str = None, drill_down: bool = False,
                      market: str = None) -> dict:
        """Process items for reprocessing."""
        # Validate item names
        unknown_items = []
        validated_items = {}

        for name, qty in items.items():
            type_id = self.reprocessor.get_type_id(name)
            if type_id is None:
                unknown_items.append(name)
            elif qty > 0:
                validated_items[name] = {
                    'type_id': type_id,
                    'quantity': int(qty),
                    'name': name
                }

        if unknown_items:
            return {
                'error': 'UNKNOWN_ITEMS',
                'details': sorted(unknown_items)
            }

        # Get API config
        api_config = None
        if api_key:
            try:
                api_config = self.api_manager.validate_key(api_key)
            except ValueError:
                return {
                    'error': 'BAD_API_KEY',
                    'details': 'Invalid API key'
                }

        if api_config:
            efficiencies = self.get_efficiency_multiplier(api_config)
        elif efficiencies is None:
            efficiencies = self.get_efficiency_multiplier()

        # Track outputs
        all_output_products: Dict[int, int] = defaultdict(int)
        total_input_volume = 0.0

        # Track lowest yield per type
        yields_used = {'ore': 1.0, 'gas': 1.0, 'scrap': 1.0}

        # Process items iteratively for drill-down
        processed_in_iteration = set()

        iteration = 0
        while validated_items and iteration < 100:
            iteration += 1
            next_batch = {}
            items_in_this_iter = set(validated_items.keys())

            for name, item_data in validated_items.items():
                type_id = item_data['type_id']
                quantity = item_data['quantity']

                volume = self.reprocessor.get_volume(type_id)
                total_input_volume += quantity * volume

                portion_size = self.reprocessor.get_portionsize(type_id)

                if portion_size == 0:
                    # Non-reprocessable item, keep as-is
                    all_output_products[type_id] += quantity
                    continue

                full_portions = quantity // portion_size
                leftover = quantity % portion_size

                if leftover > 0:
                    all_output_products[type_id] += leftover

                if full_portions == 0:
                    continue

                materials = self.reprocessor.get_materials(type_id)

                if not materials:
                    all_output_products[type_id] += full_portions * portion_size
                    continue

                reproc_type = self.get_reprocess_type(type_id)
                efficiency = efficiencies.get(reproc_type, 1.0)

                if efficiency < yields_used[reproc_type]:
                    yields_used[reproc_type] = efficiency

                # Sum materials
                material_totals: Dict[int, int] = defaultdict(int)
                for mat in materials:
                    material_totals[mat['type_id']] += mat['quantity'] * full_portions

                # Apply efficiency (round down)
                for mat_type_id, total_qty in material_totals.items():
                    processed = int(total_qty * efficiency)
                    if processed > 0:
                        all_output_products[mat_type_id] += processed

                        # Queue for drill-down if reprocessable and not already processed
                        mat_name = self.reprocessor.get_type_name(mat_type_id)
                        if mat_name and mat_name not in items_in_this_iter and drill_down:
                            # Check if it has materials (is reprocessable)
                            if self.reprocessor.get_materials(mat_type_id):
                                next_batch[mat_name] = {
                                    'type_id': mat_type_id,
                                    'quantity': processed,
                                    'name': mat_name
                                }

            validated_items = next_batch
            if not validated_items:
                break

        # Build products output
        products_output = {}

        for type_id, total_qty in all_output_products.items():
            name = self.reprocessor.get_type_name(type_id)
            if not name:
                continue
            volume = self.reprocessor.get_volume(type_id)

            buy_price = sell_price = None

            if market and market in self.reprocessor.market_prices:
                if type_id in self.reprocessor.market_prices[market]:
                    price_data = self.reprocessor.market_prices[market][type_id]
                    buy_price = price_data.get('buy')
                    sell_price = price_data.get('sell')

            products_output[name] = {
                'quantity': total_qty,
                'volume': round(total_qty * volume, 2),
                'buy': round(buy_price, 2) if buy_price is not None else None,
                'sell': round(sell_price, 2) if sell_price is not None else None
            }

        # Calculate volumes
        output_volume = sum(
            qty * self.reprocessor.get_volume(typ)
            for typ, qty in all_output_products.items()
        )

        # Build result
        result = {
            'inputs': {
                'volume': round(total_input_volume, 2),
                'buy': None,
                'sell': None,
                'buy_5pct': None,
                'sell_5pct': None
            },
            'outputs': {
                'volume': round(output_volume, 2),
                'buy': None,
                'sell': None,
                'buy_5pct': None,
                'sell_5pct': None
            },
            'yields': {
                'ore': round(yields_used['ore'] * 100, 2),
                'gas': round(yields_used['gas'] * 100, 2),
                'scrap': round(yields_used['scrap'] * 100, 2)
            },
            'products': products_output
        }

        return result


def create_reprocessing_engine(sde_path: str) -> ReprocessingEngine:
    """Factory function to create a configured ReprocessingEngine."""
    reprocessor = Reprocessor(sde_path)
    api_manager = APIKeyManager()
    return ReprocessingEngine(reprocessor, api_manager)
