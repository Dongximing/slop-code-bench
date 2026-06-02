#!/usr/bin/env python3
"""EVE Online Industrial Planner - Parse SDE and emit recipe reports."""

import argparse
import bz2
import csv
import math
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import yaml


def load_csv_bz2(filepath: Path) -> List[Dict]:
    """Load a BZ2-compressed CSV file and return list of dicts."""
    with bz2.open(filepath, 'rt', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        return list(reader)


def load_yaml(filepath: Path) -> Dict:
    """Load a YAML file and return dict."""
    with open(filepath, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


class SDEParser:
    """Parses all SDE files and builds lookup structures."""

    def __init__(self, sde_dir: Path):
        self.sde_dir = sde_dir
        self.types: Dict[int, Dict] = {}
        self.groups: Dict[int, Dict] = {}
        self.categories: Dict[int, Dict] = {}
        self.market_groups: Dict[int, Dict] = {}
        self.meta_types: Dict[int, int] = {}  # typeID -> metaGroupID
        self.meta_groups: Dict[int, str] = {}
        self.activities: Dict[int, Dict[int, int]] = {}  # typeID -> {activityID: time}
        self.products: Dict[int, Dict[int, Dict[int, int]]] = {}  # typeID -> {activityID: {productTypeID: quantity}}
        self.materials: Dict[int, Dict[int, List[Dict]]] = {}  # typeID -> {activityID: [{typeID, quantity}]}
        self.activity_names: Dict[int, str] = {}
        self.ship_volumes: Dict[str, float] = {}
        self.buildable_types: Set[int] = set()

    def load_all(self):
        """Load all SDE files."""
        self._load_types()
        self._load_groups()
        self._load_categories()
        self._load_market_groups()
        self._load_meta_types()
        self._load_meta_groups()
        self._load_activities()
        self._load_products()
        self._load_materials()
        self._load_activity_names()
        self._load_ship_volumes()
        self._compute_buildable_types()

    def _load_types(self):
        data = load_csv_bz2(self.sde_dir / 'invTypes.csv.bz2')
        for row in data:
            type_id = int(row['typeID'])
            if int(row.get('published', 0)) == 1:
                self.types[type_id] = {
                    'typeID': type_id,
                    'groupID': int(row['groupID']),
                    'typeName': row['typeName'],
                    'volume': row.get('volume', '0'),
                    'marketGroupID': int(row['marketGroupID']) if row.get('marketGroupID') and row['marketGroupID'] != 'None' else None,
                    'mass': row.get('mass', '0'),
                    'raceID': row.get('raceID'),
                    'basePrice': row.get('basePrice'),
                    'description': row.get('description', ''),
                }

    def _load_groups(self):
        data = load_csv_bz2(self.sde_dir / 'invGroups.csv.bz2')
        for row in data:
            group_id = int(row['groupID'])
            if int(row.get('published', 0)) == 1:
                self.groups[group_id] = {
                    'groupID': group_id,
                    'categoryID': int(row['categoryID']),
                    'groupName': row['groupName'],
                    'iconID': row.get('iconID'),
                }

    def _load_categories(self):
        data = load_csv_bz2(self.sde_dir / 'invCategories.csv.bz2')
        for row in data:
            cat_id = int(row['categoryID'])
            if int(row.get('published', 0)) == 1:
                self.categories[cat_id] = {
                    'categoryID': cat_id,
                    'categoryName': row['categoryName'],
                    'iconID': row.get('iconID'),
                }

    def _load_market_groups(self):
        data = load_csv_bz2(self.sde_dir / 'invMarketGroups.csv.bz2')
        for row in data:
            mg_id = int(row['marketGroupID'])
            parent_id = int(row['parentGroupID']) if row.get('parentGroupID') and row['parentGroupID'] != 'None' else None
            self.market_groups[mg_id] = {
                'marketGroupID': mg_id,
                'parentGroupID': parent_id,
                'marketGroupName': row['marketGroupName'],
                'description': row.get('description', ''),
                'iconID': row.get('iconID'),
                'hasTypes': int(row.get('hasTypes', 0)) == 1,
            }

    def _load_meta_types(self):
        data = load_csv_bz2(self.sde_dir / 'invMetaTypes.csv.bz2')
        for row in data:
            type_id = int(row['typeID'])
            meta_group_id = row.get('metaGroupID')
            if meta_group_id:
                self.meta_types[type_id] = int(meta_group_id)

    def _load_meta_groups(self):
        data = load_csv_bz2(self.sde_dir / 'invMetaGroups.csv.bz2')
        for row in data:
            mg_id = int(row['metaGroupID'])
            self.meta_groups[mg_id] = row['metaGroupName']

    def _load_activities(self):
        data = load_csv_bz2(self.sde_dir / 'industryActivity.csv.bz2')
        for row in data:
            type_id = int(row['typeID'])
            activity_id = int(row['activityID'])
            time_val = int(row['time'])
            if type_id not in self.activities:
                self.activities[type_id] = {}
            self.activities[type_id][activity_id] = time_val

    def _load_products(self):
        data = load_csv_bz2(self.sde_dir / 'industryActivityProducts.csv.bz2')
        for row in data:
            type_id = int(row['typeID'])
            activity_id = int(row['activityID'])
            product_type_id = int(row['productTypeID'])
            quantity = int(row['quantity'])
            if type_id not in self.products:
                self.products[type_id] = {}
            if activity_id not in self.products[type_id]:
                self.products[type_id][activity_id] = {}
            self.products[type_id][activity_id][product_type_id] = quantity

    def _load_materials(self):
        data = load_csv_bz2(self.sde_dir / 'industryActivityMaterials.csv.bz2')
        for row in data:
            type_id = int(row['typeID'])
            activity_id = int(row['activityID'])
            material_type_id = int(row['materialTypeID'])
            quantity = int(row['quantity'])
            if type_id not in self.materials:
                self.materials[type_id] = {}
            if activity_id not in self.materials[type_id]:
                self.materials[type_id][activity_id] = []
            self.materials[type_id][activity_id].append({
                'typeID': material_type_id,
                'quantity': quantity,
            })

    def _load_activity_names(self):
        data = load_csv_bz2(self.sde_dir / 'ramActivities.csv.bz2')
        for row in data:
            activity_id = int(row['activityID'])
            if row.get('published') == '1':
                self.activity_names[activity_id] = row['activityName']

    def _load_ship_volumes(self):
        data = load_yaml(self.sde_dir / 'ship_volumes.yaml')
        self.ship_volumes = {k: float(v) for k, v in data.items()}

    def _compute_buildable_types(self):
        """Types that have Manufacturing (1) or Reactions (8) activities are buildable."""
        for type_id, activities in self.products.items():
            if 1 in activities or 8 in activities:
                self.buildable_types.add(type_id)

    def get_type_by_name(self, name: str) -> Optional[Dict]:
        """Find a type by exact typeName (case-sensitive)."""
        for type_data in self.types.values():
            if type_data['typeName'] == name:
                return type_data
        return None

    def get_type_name(self, type_id: int) -> Optional[str]:
        """Get typeName by typeID."""
        t = self.types.get(type_id)
        return t['typeName'] if t else None

    def get_group_path(self, type_id: int) -> Tuple[str, str]:
        """Get (categoryName, groupName) for a type."""
        type_data = self.types.get(type_id)
        if not type_data:
            return ('Unknown', 'Unknown')
        group_data = self.groups.get(type_data['groupID'])
        if not group_data:
            return ('Unknown', 'Unknown')
        cat_data = self.categories.get(group_data['categoryID'])
        if not cat_data:
            return ('Unknown', group_data['groupName'])
        return (cat_data['categoryName'], group_data['groupName'])

    def get_market_group_path(self, type_id: int) -> Optional[str]:
        """Get full market group path as ' > ' joined string, or None."""
        type_data = self.types.get(type_id)
        if not type_data or not type_data.get('marketGroupID'):
            return None

        mg_id = type_data['marketGroupID']
        path_parts = []

        while mg_id is not None:
            mg_data = self.market_groups.get(mg_id)
            if not mg_data:
                break
            path_parts.append(mg_data['marketGroupName'])
            mg_id = mg_data.get('parentGroupID')

        return ' > '.join(reversed(path_parts))

    def get_tech_level(self, type_id: int) -> str:
        """Get tech level as 'Tech I', 'Tech II', or 'Tech III'."""
        meta_group_id = self.meta_types.get(type_id)
        if not meta_group_id:
            return 'Tech I'

        meta_name = self.meta_groups.get(meta_group_id, '')
        if meta_name == 'Tech II':
            return 'Tech II'
        elif meta_name == 'Tech III':
            return 'Tech III'
        else:
            return 'Tech I'

    def get_volume(self, type_id: int) -> float:
        """Get packaged volume for ships, otherwise invTypes volume."""
        type_data = self.types.get(type_id)
        if not type_data:
            return 0.0

        # Check if it's a ship
        _, group_name = self.get_group_path(type_id)
        if group_name in self.ship_volumes:
            return self.ship_volumes[group_name]

        # Non-ship items use invTypes.volume
        vol_str = type_data.get('volume', '0') or '0'
        try:
            return float(vol_str)
        except ValueError:
            return 0.0

    def is_buildable(self, type_id: int) -> bool:
        """Check if an item can be produced via industry."""
        return type_id in self.buildable_types

    def get_recipe(self, type_id: int) -> Optional[Dict]:
        """Get recipe info for a type. Returns dict with activity, output_qty, run_time, materials."""
        # Check Manufacturing first, then Reactions
        for activity_id in [1, 11]:  # 1=Manufacturing, 11=Reactions
            if activity_id in self.activities.get(type_id, {}):
                activities = self.activities[type_id]
                products = self.products.get(type_id, {})
                materials = self.materials.get(type_id, {})

                activity_name = self.activity_names.get(activity_id, 'Unknown')
                output_qty = 0
                if activity_id in products:
                    for prod_id, qty in products[activity_id].items():
                        output_qty = qty  # Should be only one product per activity

                run_time_seconds = activities.get(activity_id, 0)
                run_time_minutes = math.ceil(run_time_seconds / 60)

                mat_list = []
                if activity_id in materials:
                    for mat in materials[activity_id]:
                        mat_list.append({
                            'typeID': mat['typeID'],
                            'quantity': mat['quantity'],
                        })

                if mat_list or activity_name in ['Manufacturing', 'Reactions']:
                    return {
                        'activity': activity_name,
                        'output_qty': output_qty,
                        'run_time': run_time_minutes,
                        'materials': mat_list,
                    }

        return None


def generate_report(sde_parser: SDEParser, lookup_name: str) -> str:
    """Generate the full recipe report for a product or blueprint."""

    # First, try to find the type by exact name
    type_data = sde_parser.get_type_by_name(lookup_name)

    is_blueprint_lookup = False
    # Track if we need to use a blueprint's recipe (for display)
    display_blueprint_type_id = None
    # Track the product type we want to display
    display_product_type_id = None

    # Check if the found item is a blueprint (has "Blueprint" in name)
    if type_data and 'Blueprint' in type_data.get('typeName', ''):
        is_blueprint_lookup = True
        # Get the product type from this blueprint
        products = sde_parser.products.get(type_data.get('typeID'), {})
        for activity_id, prod_dict in products.items():
            if prod_dict:  # Should have only one product
                display_product_type_id = next(iter(prod_dict.keys()))
                break

    if not type_data:
        # Try as blueprint name - the product name is the type name without " Blueprint"
        if lookup_name.endswith(' Blueprint'):
            is_blueprint_lookup = True
            product_name = lookup_name[:-10]  # Remove " Blueprint"
            type_data = sde_parser.get_type_by_name(product_name)

    if not type_data:
        # Could also be looking for a blueprint that isn't named with " Blueprint"
        # Try to find any blueprint that produces this item
        for t in sde_parser.types.values():
            if 'Blueprint' in t['typeName']:
                # Check if this blueprint produces our lookup_name
                type_id = t['typeID']
                products = sde_parser.products.get(type_id, {})
                for activity_id, prod_dict in products.items():
                    for prod_type_id, _ in prod_dict.items():
                        prod_name = sde_parser.get_type_name(prod_type_id)
                        if prod_name == lookup_name:
                            type_data = t
                            display_blueprint_type_id = type_id
                            # Find the product type for display
                            display_product_type_id = prod_type_id
                            break
                    if type_data:
                        break
                if type_data:
                    break

    if not type_data:
        return f"Error: Item '{lookup_name}' not found or not published.\n"

    type_id = type_data['typeID']

    # If we identified a blueprint (either from name or content), find the recipe from it
    if is_blueprint_lookup and display_product_type_id:
        display_blueprint_type_id = type_id

    # If we have a blueprint to use, get recipe from it
    if display_blueprint_type_id:
        recipe = sde_parser.get_recipe(display_blueprint_type_id)
    else:
        # Get recipe - either for this item directly, or find blueprint that produces it
        recipe = sde_parser.get_recipe(type_id)

        if not recipe:
            # Try to find a blueprint that produces this item
            for t in sde_parser.types.values():
                products = sde_parser.products.get(t['typeID'], {})
                for activity_id, prod_dict in products.items():
                    if type_id in prod_dict:
                        recipe = sde_parser.get_recipe(t['typeID'])
                        if recipe:
                            break
                if recipe:
                    break

    if not recipe:
        return f"Error: No industry activity found for '{lookup_name}'.\n"

    # Determine what to display: if we have a blueprint, show the product info
    display_type_id = type_id
    display_type_data = type_data
    if display_product_type_id:
        # We looked up a blueprint or found one, show the product
        product_data = sde_parser.types.get(display_product_type_id)
        if product_data:
            display_type_data = product_data
            display_type_id = display_product_type_id

    # Build the report
    lines = []

    # ITEM line - show the product, not the blueprint
    lines.append(f"ITEM: {display_type_data['typeName']} ({display_type_id})")

    # Group line - use display_type_id for product info
    cat_name, group_name = sde_parser.get_group_path(display_type_id)
    lines.append(f"Group: {cat_name} > {group_name}")

    # Market Group line - use display_type_id for product info
    mg_path = sde_parser.get_market_group_path(display_type_id)
    lines.append(f"Market Group: {mg_path if mg_path else 'None'}")

    # Tech Level - use display_type_id for product info
    tech_level = sde_parser.get_tech_level(display_type_id)
    lines.append(f"Tech Level: {tech_level}")

    # Volume - use display_type_id for product info
    volume = sde_parser.get_volume(display_type_id)
    lines.append(f"Volume: {volume:.2f}")

    # Recipe header
    lines.append("")
    lines.append("Recipe:")
    lines.append(f"Activity: {recipe['activity']}")
    lines.append(f"Output Quantity: {recipe['output_qty']}")
    lines.append(f"Run Time: {recipe['run_time']}")

    # Materials table
    lines.append("| Item | Quantity | Buildable |")
    lines.append("|:-:|:---:|---:|")

    # Sort materials alphabetically by item name (case-insensitive)
    materials_with_names = []
    for mat in recipe['materials']:
        mat_name = sde_parser.get_type_name(mat['typeID']) or 'Unknown'
        buildable = sde_parser.is_buildable(mat['typeID'])
        materials_with_names.append((mat_name.lower(), mat_name, mat['quantity'], buildable))

    # Sort by item name (case-insensitive)
    materials_with_names.sort(key=lambda x: x[0])

    for _, mat_name, qty, buildable in materials_with_names:
        buildable_str = 'Yes' if buildable else 'No'
        lines.append(f"| {mat_name} | {qty} | {buildable_str} |")

    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(
        description='EVE Online Industrial Planner - Generate recipe reports from SDE'
    )
    parser.add_argument('command', choices=['recipe'], help='Command to run')
    parser.add_argument('target', help='Product or Blueprint name (exact match, case-sensitive)')
    parser.add_argument('--sde', required=True, help='Path to SDE directory')

    args = parser.parse_args()

    sde_path = Path(args.sde)
    if not sde_path.exists():
        print(f"Error: SDE directory '{args.sde}' does not exist.", file=sys.stderr)
        sys.exit(1)

    # Parse all SDE files
    sde = SDEParser(sde_path)
    sde.load_all()

    if args.command == 'recipe':
        report = generate_report(sde, args.target)
        print(report, end='')


if __name__ == '__main__':
    main()
