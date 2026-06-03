#!/usr/bin/env python3
"""
EVE Online Industry Recipe Planner

Parse the Static Data Export (SDE) and emit a deterministic recipe report
for a target product or blueprint.
"""

import argparse
import bz2
import csv
import math
import sys
import yaml
from pathlib import Path


def load_csv(path: Path) -> list[dict]:
    """Load a CSV file (potentially bz2 compressed) and return list of dicts."""
    if path.suffixes[-1] == '.bz2':
        open_func = lambda p: bz2.open(p, 'rt')
    else:
        open_func = lambda p: open(p, 'r', encoding='utf-8')
    with open_func(path) as f:
        return list(csv.DictReader(f))


class SDE:
    """Container and accessor for all Static Data Export data."""

    def __init__(self, sde_dir: Path):
        self.sde_dir = sde_dir

        # Maps
        self.types_by_id: dict[int, dict] = {}
        self.types_by_name: dict[str, dict] = {}
        self.groups_by_id: dict[int, dict] = {}
        self.categories_by_id: dict[int, dict] = {}
        self.market_groups_by_id: dict[int, dict] = {}
        self.meta_groups_by_id: dict[int, dict] = {}
        self.meta_types: list[dict] = []
        self.industry_activities: list[dict] = []
        self.products: list[dict] = []
        self.materials: list[dict] = []
        self.ship_volumes: dict[str, float] = {}

        # Derived
        self.type_to_meta: dict[int, dict] = {}  # typeID -> meta entry (child)
        self.type_to_blueprint: dict[int, dict] = {}  # productTypeID -> blueprint typeID entry (parent)

        self._load_all()

    def _load_all(self):
        self._load_types()
        self._load_groups()
        self._load_categories()
        self._load_market_groups()
        self._load_meta_groups()
        self._load_meta_types()
        self._load_industry_activities()
        self._load_products()
        self._load_materials()
        self._load_ship_volumes()
        self._derive_relations()

    def _load_types(self):
        rows = load_csv(self.sde_dir / 'invTypes.csv.bz2')
        for row in rows:
            tid = int(row['typeID'])
            self.types_by_id[tid] = row
            name = row['typeName']
            if name:  # skip system placeholder
                self.types_by_name[name] = row

    def _load_groups(self):
        rows = load_csv(self.sde_dir / 'invGroups.csv.bz2')
        for row in rows:
            gid = int(row['groupID'])
            self.groups_by_id[gid] = row

    def _load_categories(self):
        rows = load_csv(self.sde_dir / 'invCategories.csv.bz2')
        for row in rows:
            cid = int(row['categoryID'])
            self.categories_by_id[cid] = row

    def _load_market_groups(self):
        rows = load_csv(self.sde_dir / 'invMarketGroups.csv.bz2')
        for row in rows:
            mgid = int(row['marketGroupID'])
            self.market_groups_by_id[mgid] = row

    def _load_meta_groups(self):
        rows = load_csv(self.sde_dir / 'invMetaGroups.csv.bz2')
        for row in rows:
            mgid = int(row['metaGroupID'])
            self.meta_groups_by_id[mgid] = row

    def _load_meta_types(self):
        self.meta_types = load_csv(self.sde_dir / 'invMetaTypes.csv.bz2')
        # Build lookup: typeID -> meta entry
        for entry in self.meta_types:
            tid = int(entry['typeID'])
            self.type_to_meta[tid] = entry

    def _load_industry_activities(self):
        self.industry_activities = load_csv(self.sde_dir / 'industryActivity.csv.bz2')

    def _load_products(self):
        self.products = load_csv(self.sde_dir / 'industryActivityProducts.csv.bz2')

    def _load_materials(self):
        self.materials = load_csv(self.sde_dir / 'industryActivityMaterials.csv.bz2')

    def _load_ship_volumes(self):
        path = self.sde_dir / 'ship_volumes.yaml'
        if path.exists():
            with open(path, 'r', encoding='utf-8') as f:
                self.ship_volumes = yaml.safe_load(f) or {}

    def _derive_relations(self):
        # For products, find their blueprint
        # productTypeID (from industryActivityProducts) corresponds to a typeID in invTypes
        # The parent typeID (from industryActivityProducts entry) is the blueprint typeID
        # Build lookup: productTypeID -> blueprint typeID entry
        for prod in self.products:
            parent_tid = int(prod['typeID'])       # blueprint typeID
            child_tid = int(prod['productTypeID']) # product typeID
            # Only store if we have a clear relationship
            if child_tid not in self.type_to_blueprint:
                self.type_to_blueprint[child_tid] = {
                    'blueprint': self.types_by_id.get(parent_tid),
                    'product': self.types_by_id.get(child_tid),
                    'product_entry': prod,
                }

    def get_type_by_name(self, name: str) -> dict | None:
        """Get type entry by exact name (case-sensitive)."""
        return self.types_by_name.get(name)

    def get_group_path(self, type_entry: dict) -> str:
        """Get category > group hierarchy for a type."""
        gid = int(type_entry['groupID'])
        group = self.groups_by_id.get(gid, {})
        cat_id = int(group.get('categoryID', 0))
        category = self.categories_by_id.get(cat_id, {})
        cat_name = category.get('categoryName', 'Unknown')
        group_name = group.get('groupName', 'Unknown')
        return f"{cat_name} > {group_name}"

    def get_market_group_path(self, type_entry: dict) -> str | None:
        """Get full market group path from root to leaf, or None."""
        mgid_str = type_entry.get('marketGroupID')
        if not mgid_str or mgid_str == 'None':
            return None
        mgid = int(float(mgid_str)) if mgid_str else None
        if mgid is None or mgid not in self.market_groups_by_id:
            return None

        # Build path from leaf to root
        path_parts = []
        current_id = mgid
        while current_id is not None:
            mg = self.market_groups_by_id.get(current_id)
            if mg is None:
                break
            path_parts.append(mg['marketGroupName'])
            parent_str = mg.get('parentGroupID')
            if parent_str and parent_str != 'None':
                current_id = int(float(parent_str))
            else:
                current_id = None

        # Reverse to get root to leaf
        path_parts.reverse()
        return ' > '.join(path_parts)

    def get_tech_level(self, type_entry: dict) -> str:
        """Determine Tech Level as Tech I, Tech II, or Tech III."""
        tid = int(type_entry['typeID'])
        meta_entry = self.type_to_meta.get(tid)
        if meta_entry:
            mgid = int(meta_entry['metaGroupID'])
            mg = self.meta_groups_by_id.get(mgid, {})
            mg_name = mg.get('metaGroupName', '')
            if mg_name == 'Tech I':
                return 'Tech I'
            elif mg_name == 'Tech II':
                return 'Tech II'
            elif mg_name == 'Tech III':
                return 'Tech III'
        # Default to Tech I if not found
        return 'Tech I'

    def get_volume(self, type_entry: dict) -> float:
        """Get volume, using packaged volume for ships."""
        gid = int(type_entry['groupID'])
        group = self.groups_by_id.get(gid, {})
        group_name = group.get('groupName', '')

        # Check if it's a ship group
        if group_name in self.ship_volumes:
            return self.ship_volumes[group_name]

        # Non-ship: use invTypes.volume
        vol_str = type_entry.get('volume', '0')
        if vol_str and vol_str not in ('None', '0E-10'):
            try:
                return float(vol_str)
            except ValueError:
                pass
        return 0.0

    def get_recipe(self, type_entry: dict) -> dict | None:
        """Get manufacturing or reactions recipe for a type."""
        tid = int(type_entry['typeID'])

        # Find the blueprint if this is a product
        relation = self.type_to_blueprint.get(tid)

        if not relation:
            # Could be a blueprint itself - check if it has materials directly
            # A blueprint typeID may have industryActivity entries
            activity_rows = [a for a in self.industry_activities if int(a['typeID']) == tid]
            if not activity_rows:
                return None
        else:
            # Use the blueprint
            blueprint = relation['blueprint']
            product_entry = relation['product_entry']
            btid = int(blueprint['typeID'])
            activity_rows = [a for a in self.industry_activities if int(a['typeID']) == btid]

        if not activity_rows:
            return None

        # Get Manufacturing (1) or Reactions (11)
        for act in activity_rows:
            aid = int(act['activityID'])
            if aid in (1, 11):  # Manufacturing or Reactions
                break
        else:
            return None

        # Determine activity name
        activity_name = 'Manufacturing' if aid == 1 else 'Reactions'

        # Get output quantity
        if relation:
            output_qty = int(product_entry['quantity'])
        else:
            # Direct blueprint - find its product
            prod_rows = [p for p in self.products if int(p['typeID']) == tid and int(p['activityID']) == aid]
            if prod_rows:
                output_qty = int(prod_rows[0]['quantity'])
            else:
                output_qty = 1

        # Get time
        time_minutes = int(math.ceil(float(act['time'])))

        # Get materials - use blueprint typeID for products
        if relation:
            lookup_tid = int(blueprint['typeID'])
        else:
            lookup_tid = tid
        mat_rows = [m for m in self.materials if int(m['typeID']) == lookup_tid and int(m['activityID']) == aid]
        materials = []
        for mat in mat_rows:
            mat_tid = int(mat['materialTypeID'])
            mat_entry = self.types_by_id.get(mat_tid)
            if mat_entry and mat_entry.get('published') == '1':
                qty = int(mat['quantity'])
                # Check if material is buildable (can be produced via industry)
                # A material is buildable if it has a blueprint (is a product) OR is a blueprint itself with materials
                buildable = False
                if mat_tid in self.type_to_blueprint:
                    buildable = True
                else:
                    # Check if it has its own materials
                    mat_has_mats = [m for m in self.materials if int(m['typeID']) == mat_tid and int(m['activityID']) in (1, 11)]
                    if mat_has_mats:
                        buildable = True
                materials.append({
                    'name': mat_entry['typeName'],
                    'typeID': mat_tid,
                    'quantity': qty,
                    'buildable': buildable,
                })

        # Sort materials alphabetically by name (case-insensitive)
        materials.sort(key=lambda m: m['name'].lower())

        return {
            'activity': activity_name,
            'output_quantity': output_qty,
            'run_time': time_minutes,
            'materials': materials,
        }


def format_recipe_report(sde: SDE, type_entry: dict) -> str:
    """Generate the canonical recipe report block."""
    tid = type_entry['typeID']
    name = type_entry['typeName']

    group_path = sde.get_group_path(type_entry)
    market_path = sde.get_market_group_path(type_entry)
    tech_level = sde.get_tech_level(type_entry)
    volume = sde.get_volume(type_entry)

    lines = []
    lines.append(f"ITEM: {name} ({tid})")
    lines.append(f"Group: {group_path}")
    if market_path:
        lines.append(f"Market Group: {market_path}")
    else:
        lines.append("Market Group: None")
    lines.append(f"Tech Level: {tech_level}")
    lines.append(f"Volume: {volume:.2f}")

    recipe = sde.get_recipe(type_entry)

    if recipe:
        lines.append("")
        lines.append("Recipe:")
        lines.append(f"Activity: {recipe['activity']}")
        lines.append(f"Output Quantity: {recipe['output_quantity']}")
        lines.append(f"Run Time: {recipe['run_time']}")
        lines.append("| Item | Quantity | Buildable |")
        lines.append("|:-:|:---:|---:|")
        for mat in recipe['materials']:
            buildable_str = 'Yes' if mat['buildable'] else 'No'
            lines.append(f"| {mat['name']} | {mat['quantity']} | {buildable_str} |")

    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(
        description='EVE Online Industry Recipe Planner'
    )
    parser.add_argument(
        'recipe_target',
        help='Product or Blueprint name (exact match, case-sensitive)'
    )
    parser.add_argument(
        '--sde',
        required=True,
        type=Path,
        help='Path to SDE directory containing data files'
    )

    args = parser.parse_args()

    if not args.sde.exists():
        print(f"Error: SDE directory '{args.sde}' does not exist", file=sys.stderr)
        sys.exit(1)

    sde = SDE(args.sde)
    type_entry = sde.get_type_by_name(args.recipe_target)

    if not type_entry:
        print(f"Error: Item '{args.recipe_target}' not found in SDE", file=sys.stderr)
        sys.exit(1)

    # Check published
    if type_entry.get('published') != '1':
        print(f"Error: Item '{args.recipe_target}' is not published", file=sys.stderr)
        sys.exit(1)

    report = format_recipe_report(sde, type_entry)
    print(report)


if __name__ == '__main__':
    main()
