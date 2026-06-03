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
    """Load plain YAML file."""
    with open(path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def load_csv_bz2(path):
    """Load bz2 compressed CSV file with headers."""
    with bz2.open(path, 'rt', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        return list(reader)


class SDE:
    """SDE data holder with lookup indexes."""

    def __init__(self, sde_dir):
        self.sde_dir = Path(sde_dir)
        self._load_all()

    def _load_all(self):
        # Load invTypes (only published items)
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

        # Load invGroups
        groups_path = self.sde_dir / "invGroups.csv.bz2"
        raw_groups = load_csv_bz2(groups_path)
        self.groups_by_id = {}
        for g in raw_groups:
            if g['published'] == '1':
                self.groups_by_id[int(g['groupID'])] = g

        # Load invCategories
        cats_path = self.sde_dir / "invCategories.csv.bz2"
        raw_cats = load_csv_bz2(cats_path)
        self.categories_by_id = {}
        for c in raw_cats:
            if c['published'] == '1':
                self.categories_by_id[int(c['categoryID'])] = c

        # Load invMarketGroups
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

        # Load invMetaGroups
        metag_path = self.sde_dir / "invMetaGroups.csv.bz2"
        raw_metag = load_csv_bz2(metag_path)
        self.meta_groups_by_id = {}
        for mg in raw_metag:
            self.meta_groups_by_id[int(mg['metaGroupID'])] = mg

        # Load invMetaTypes (Tech II/III detection)
        metatypes_path = self.sde_dir / "invMetaTypes.csv.bz2"
        raw_metatypes = load_csv_bz2(metatypes_path)
        self.meta_types_by_type = {}
        for mt in raw_metatypes:
            type_id = int(mt['typeID'])
            self.meta_types_by_type[type_id] = mt

        # Load industry data
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

        # Load ship volumes (for packaged ship volumes)
        ship_vol_path = self.sde_dir / "ship_volumes.yaml"
        if ship_vol_path.exists():
            self.ship_volumes = load_yaml(ship_vol_path)
        else:
            self.ship_volumes = {}

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

        # Check if ship in ship_volumes (packaged volume)
        g = self.groups_by_id.get(group_id, {})
        group_name = g.get('groupName', '')
        if group_name in self.ship_volumes:
            # Return float for ship volumes (preserve decimal precision)
            return float(self.ship_volumes[group_name])

        # For non-ship items, use invTypes.volume as-is
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

        # Build path from leaf to root
        path = []
        current_id = mg_id
        while current_id is not None:
            mg = self.market_groups_by_id.get(current_id)
            if not mg:
                break
            path.append(mg['marketGroupName'])
            parent_id = mg.get('parentGroupID')
            current_id = parent_id

        # Reverse to get root -> leaf
        path.reverse()
        return path


def find_target(sde: SDE, name: str) -> int:
    """Find typeID by exact typeName (case-sensitive). Raises if not found."""
    if name in sde.types_by_name:
        return int(sde.types_by_name[name]['typeID'])
    raise ValueError(f"Unknown product or blueprint: {name}")


def get_recipe(sde: SDE, target_type_id: int, target_name: str) -> dict:
    """Extract recipe for a product or blueprint."""

    # Determine if target is a product or a blueprint
    blueprint_id = sde.get_blueprint_for_product(target_type_id)
    if blueprint_id is not None:
        # Target is a product, find its blueprint
        bp_id = blueprint_id
        is_product = True
    else:
        # Target might be a blueprint directly, check if it produces something
        bp_id = target_type_id
        is_product = False

    # Check for Manufacturing (activity 1) and Reactions (activity 11)
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

    # Get products (output)
    products = sde.get_products(bp_id, activity_id)
    if not products:
        raise ValueError(f"No products defined for blueprint: bp_id={bp_id}")

    # For multiple products, find the one matching our target if we started from product name
    if len(products) == 1:
        output_qty = products[0]['quantity']
        output_type_id = products[0]['productTypeID']
        output_name = sde.types_by_id[output_type_id]['typeName']
    else:
        # Multiple products, pick matching target if we searched by product name
        if is_product:
            matching = [p for p in products if p['productTypeID'] == target_type_id]
            if matching:
                output_qty = matching[0]['quantity']
                output_type_id = matching[0]['productTypeID']
                output_name = target_name
            else:
                # Target is a blueprint name with multiple outputs; pick first
                output_qty = products[0]['quantity']
                output_type_id = products[0]['productTypeID']
                output_name = sde.types_by_id[output_type_id]['typeName']
        else:
            # Target is a blueprint directly, pick first output
            output_qty = products[0]['quantity']
            output_type_id = products[0]['productTypeID']
            output_name = sde.types_by_id[output_type_id]['typeName']

    # Get run time (seconds) and convert to minutes, rounded up
    time_sec = sde.get_activity_time(bp_id, activity_id)
    if time_sec is None:
        raise ValueError(f"No activity time for blueprint {bp_id}, activity {activity_id}")
    run_time_min = math.ceil(time_sec / 60)

    # Get materials
    materials = sde.get_materials(bp_id, activity_id)
    material_list = []
    for m in materials:
        mat_type_id = m['materialTypeID']
        mat_t = sde.types_by_id.get(mat_type_id, {})
        mat_name = mat_t.get('typeName', f"Unknown ({mat_type_id})")
        buildable = sde.is_buildable(mat_type_id)
        material_list.append({
            'typeID': mat_type_id,
            'name': mat_name,
            'quantity': m['quantity'],
            'buildable': 'Yes' if buildable else 'No'
        })

    # Determine volume and whether it's a ship
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


def format_recipe(recipe):
    """Format recipe as the canonical output block."""
    lines = []
    lines.append(f"ITEM: {recipe['item_name']} ({recipe['item_type_id']})")
    lines.append(f"Group: {recipe['group'][0]} > {recipe['group'][1]}")
    if recipe['market_group_path']:
        lines.append(f"Market Group: {' > '.join(recipe['market_group_path'])}")
    else:
        lines.append("Market Group: None")
    lines.append(f"Tech Level: {recipe['tech_level']}")
    # Volume: for ships show with 2 decimal places; for others strip trailing zeros
    vol = recipe['volume']
    if recipe.get('is_ship', False):
        lines.append(f"Volume: {vol:.2f}")
    else:
        # Use Decimal for exact string representation
        d = Decimal(str(vol)).normalize()
        vol_str = format(d, 'f')
        # Remove trailing zeros after decimal
        if '.' in vol_str:
            vol_str = vol_str.rstrip('0').rstrip('.')
        lines.append(f"Volume: {vol_str}")
    lines.append("")
    lines.append("Recipe:")
    lines.append(f"Activity: {recipe['activity']}")
    lines.append(f"Output Quantity: {recipe['output_quantity']}")
    lines.append(f"Run Time: {recipe['run_time']}")
    lines.append("| Item | Quantity | Buildable |")
    lines.append("|:-:|:---:|---:|")
    for m in recipe['materials']:
        lines.append(f"| {m['name']} | {m['quantity']} | {m['buildable']} |")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="EVE Online industry recipe generator",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    recipe_parser = subparsers.add_parser("recipe", help="Generate recipe for a product or blueprint")
    recipe_parser.add_argument("name", help="Product or Blueprint name (exact, case-sensitive)")
    recipe_parser.add_argument("--sde", required=True, help="Path to SDE directory")

    args = parser.parse_args()

    sde = SDE(args.sde)
    target_type_id = find_target(sde, args.name)
    recipe = get_recipe(sde, target_type_id, args.name)
    output = format_recipe(recipe)
    print(output)


if __name__ == "__main__":
    main()
