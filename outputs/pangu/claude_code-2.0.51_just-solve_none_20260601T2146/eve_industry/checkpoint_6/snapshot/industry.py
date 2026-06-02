#!/usr/bin/env python3
"""EVE Online Industrial Planner - Parse SDE and emit recipe reports."""

import argparse
import bz2
import csv
import math
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import yaml


def load_csv_bz2(filepath: Path):
    with bz2.open(filepath, 'rt', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def load_yaml_file(filepath: Path):
    with open(filepath, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


@dataclass
class BuildParams:
    item_name: str
    runs_per_job: int
    num_jobs: int
    me: int = 0
    te: int = 0
    decryptor: str = 'None'
    relic: str = None
    build_type: str = 'normal'
    build_all: bool = False


@dataclass
class TargetItem:
    """Represents a stock target item."""
    name: str
    target_qty: int
    start_qty: int = 0
    me: int = 0
    te: int = 0
    build_type: str = 'normal'  # 'normal', 'invention', 're'
    needs_to_build: bool = True
    runs_needed: int = 0
    bid: int = None
    tid: int = None


@dataclass
class SDEData:
    types: dict = field(default_factory=dict)
    groups: dict = field(default_factory=dict)
    categories: dict = field(default_factory=dict)
    market_groups: dict = field(default_factory=dict)
    meta_types: dict = field(default_factory=dict)
    meta_groups: dict = field(default_factory=dict)
    activities: dict = field(default_factory=lambda: defaultdict(dict))
    products: dict = field(default_factory=lambda: defaultdict(lambda: defaultdict(dict)))
    materials: dict = field(default_factory=lambda: defaultdict(lambda: defaultdict(list)))
    probabilities: dict = field(default_factory=lambda: defaultdict(lambda: defaultdict(dict)))
    skills: dict = field(default_factory=lambda: defaultdict(lambda: defaultdict(list)))
    activity_names: dict = field(default_factory=dict)
    ship_volumes: dict = field(default_factory=dict)
    buildable_types: set = field(default_factory=set)
    inventable_by_product: dict = field(default_factory=dict)
    blueprint_names: dict = field(default_factory=dict)


INVENTION = 8
MANUFACTURING = 1
REACTION = 11


def parse_sde(sde_dir: Path) -> SDEData:
    data = SDEData()

    for row in load_csv_bz2(sde_dir / 'invTypes.csv.bz2'):
        if int(row.get('published', 0)) != 1:
            continue
        tid = int(row['typeID'])
        data.types[tid] = {
            'typeID': tid,
            'groupID': int(row['groupID']),
            'typeName': row['typeName'],
            'volume': row.get('volume', '0'),
            'marketGroupID': (int(row['marketGroupID'])
                              if row.get('marketGroupID') and row['marketGroupID'] != 'None' else None),
            'mass': row.get('mass', '0'),
            'raceID': row.get('raceID'),
            'basePrice': row.get('basePrice'),
            'description': row.get('description', ''),
        }

    for row in load_csv_bz2(sde_dir / 'invGroups.csv.bz2'):
        if int(row.get('published', 0)) != 1:
            continue
        gid = int(row['groupID'])
        data.groups[gid] = {
            'groupID': gid,
            'categoryID': int(row['categoryID']),
            'groupName': row['groupName'],
            'iconID': row.get('iconID'),
        }

    for row in load_csv_bz2(sde_dir / 'invCategories.csv.bz2'):
        if int(row.get('published', 0)) != 1:
            continue
        cid = int(row['categoryID'])
        data.categories[cid] = {
            'categoryID': cid,
            'categoryName': row['categoryName'],
            'iconID': row.get('iconID'),
        }

    for row in load_csv_bz2(sde_dir / 'invMarketGroups.csv.bz2'):
        mg_id = int(row['marketGroupID'])
        parent_id = (int(row['parentGroupID'])
                     if row.get('parentGroupID') and row['parentGroupID'] != 'None' else None)
        data.market_groups[mg_id] = {
            'marketGroupID': mg_id,
            'parentGroupID': parent_id,
            'marketGroupName': row['marketGroupName'],
            'description': row.get('description', ''),
            'iconID': row.get('iconID'),
            'hasTypes': int(row.get('hasTypes', 0)) == 1,
        }

    for row in load_csv_bz2(sde_dir / 'invMetaTypes.csv.bz2'):
        if row.get('metaGroupID'):
            data.meta_types[int(row['typeID'])] = int(row['metaGroupID'])

    for row in load_csv_bz2(sde_dir / 'invMetaGroups.csv.bz2'):
        data.meta_groups[int(row['metaGroupID'])] = row['metaGroupName']

    for row in load_csv_bz2(sde_dir / 'industryActivity.csv.bz2'):
        data.activities[int(row['typeID'])][int(row['activityID'])] = int(row['time'])

    for row in load_csv_bz2(sde_dir / 'industryActivityProducts.csv.bz2'):
        tid = int(row['typeID'])
        aid = int(row['activityID'])
        pid = int(row['productTypeID'])
        data.products[tid][aid][pid] = int(row['quantity'])

    for row in load_csv_bz2(sde_dir / 'industryActivityMaterials.csv.bz2'):
        tid = int(row['typeID'])
        aid = int(row['activityID'])
        data.materials[tid][aid].append({
            'typeID': int(row['materialTypeID']),
            'quantity': int(row['quantity'])
        })

    for row in load_csv_bz2(sde_dir / 'industryActivityProbabilities.csv.bz2'):
        tid = int(row['typeID'])
        aid = int(row['activityID'])
        pid = int(row['productTypeID'])
        data.probabilities[tid][aid][pid] = float(row['probability'])

    for row in load_csv_bz2(sde_dir / 'industryActivitySkills.csv.bz2'):
        tid = int(row['typeID'])
        aid = int(row['activityID'])
        data.skills[tid][aid].append({
            'skillID': int(row['skillID']),
            'level': int(row['level'])
        })

    for row in load_csv_bz2(sde_dir / 'ramActivities.csv.bz2'):
        if row.get('published') == '1':
            data.activity_names[int(row['activityID'])] = row['activityName']

    data.ship_volumes = {k: float(v) for k, v in load_yaml_file(sde_dir / 'ship_volumes.yaml').items()}

    for tid, acts in data.products.items():
        if 1 in acts or 8 in acts or 11 in acts:
            data.buildable_types.add(tid)
        if 8 in acts:
            for pid in acts[8]:
                data.inventable_by_product[pid] = (tid, 8)

    for tid, tdata in data.types.items():
        if 'Blueprint' in tdata['typeName']:
            data.blueprint_names[tdata['typeName']] = tid

    return data


def _get_type_name(data: SDEData, tid: int) -> str:
    return data.types.get(tid, {}).get('typeName', '')


def get_group_path(data: SDEData, tid: int):
    tdata = data.types.get(tid)
    if not tdata:
        return 'Unknown', 'Unknown'
    gdata = data.groups.get(tdata['groupID'])
    if not gdata:
        return 'Unknown', 'Unknown'
    if cdata := data.categories.get(gdata['categoryID']):
        return cdata['categoryName'], gdata['groupName']
    return 'Unknown', gdata['groupName']


def get_market_group_path(data: SDEData, tid: int):
    tdata = data.types.get(tid)
    if not tdata or not tdata.get('marketGroupID'):
        return None
    path_parts = []
    mg_id = tdata['marketGroupID']
    while mg_id is not None:
        mgdata = data.market_groups.get(mg_id)
        if not mgdata:
            break
        path_parts.append(mgdata['marketGroupName'])
        mg_id = mgdata.get('parentGroupID')
    return ' > '.join(reversed(path_parts)) if path_parts else None


def get_tech_level(data: SDEData, tid: int) -> str:
    meta_id = data.meta_types.get(tid)
    if not meta_id:
        return 'Tech I'
    meta_name = data.meta_groups.get(meta_id, '')
    return meta_name if meta_name in ('Tech II', 'Tech III') else 'Tech I'


def get_volume(data: SDEData, tid: int) -> float:
    tdata = data.types.get(tid)
    if not tdata:
        return 0.0
    _, gname = get_group_path(data, tid)
    if gname in data.ship_volumes:
        return data.ship_volumes[gname]
    try:
        return float(tdata.get('volume') or '0')
    except ValueError:
        return 0.0


def get_recipe(data: SDEData, tid: int, me: int = 0, activity_id: int = MANUFACTURING):
    acts = data.products.get(tid, {})
    if activity_id not in acts:
        return None

    time_raw = data.activities.get(tid, {}).get(activity_id, 0)
    materials = data.materials.get(tid, {}).get(activity_id, [])

    if me > 0:
        materials = [
            {'typeID': m['typeID'], 'quantity': math.ceil(max(1, m['quantity'] * (1 - me / 100))),
             'original_quantity': m['quantity']} for m in materials
        ]
    else:
        materials = [
            {'typeID': m['typeID'], 'quantity': m['quantity'], 'original_quantity': m['quantity']}
            for m in materials
        ]

    return {
        'activity': data.activity_names.get(activity_id, f'Activity {activity_id}'),
        'output_qty': sum(acts[activity_id].values()),
        'run_time': round(time_raw / 60),
        'materials': materials
    }


def get_invention_data(data: SDEData, pid: int, skill_level: int):
    bid, _ = data.inventable_by_product.get(pid, (None, None))
    if not bid:
        return None

    time_minutes = math.ceil(data.activities.get(bid, {}).get(INVENTION, 0) / 60)

    dc_materials = data.materials.get(bid, {}).get(INVENTION, [])
    datacores = []
    for mat in dc_materials:
        mname = _get_type_name(data, mat['typeID'])
        if mname and mname.startswith('Datacore - '):
            datacores.append({
                'typeID': mat['typeID'], 'name': mname, 'quantity': mat['quantity']
            })

    skill_list = data.skills.get(bid, {}).get(INVENTION, [])
    skills = [{'name': _get_type_name(data, s['skillID']), 'required_level': s['level']}
              for s in skill_list]

    base_prob = data.probabilities.get(bid, {}).get(INVENTION, {}).get(pid, 0.0)

    return {
        'invention_blueprint_id': bid,
        'time_minutes': time_minutes,
        'datacores': datacores,
        'skills': skills,
        'base_probability': base_prob,
        'tech_level': get_tech_level(data, pid)
    }


def calculate_probability(base_prob: float, skill_level: int, decryptor: str) -> float:
    mult = base_prob * (1 + 7 * skill_level / 120)
    dec = DECRYPTORS.get(decryptor, (0, 0, 0, 0))
    return max(0.0, min(1.0, mult * (1 + dec[0] / 100.0)))


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


TECHIII_RELICS = {'Intact': 0.26, 'Malfunctioning': 0.21, 'Wrecked': 0.14}
TECHIII_BASE_RUNS = {'Intact': 20, 'Malfunctioning': 10, 'Wrecked': 3}


def split_job_runs(total_runs: int, split_days: float, days_per_run: float):
    if total_runs <= 0 or days_per_run == 0:
        return [(total_runs, 1)]

    max_days_threshold = min(30, split_days)

    if total_runs * days_per_run <= max_days_threshold:
        return [(total_runs, 1)]

    max_runs = int(max_days_threshold // days_per_run)
    if max_runs < 2:
        return [(total_runs, 1)]

    N = total_runs
    if N <= max_runs:
        return [(N, 1)]

    t = (N + max_runs - 1) // max_runs
    B = N // t
    x = N % t

    if B + 1 == max_runs and x > t / 2 and N // (t + 1) + 1 < max_runs:
        t = t + 1
        B = N // t
        x = N % t

    result = []
    if x > 0:
        result.append((B + 1, x))
    if t - x > 0:
        result.append((B, t - x))

    return result


def parse_build_string(build_str: str):
    parts = build_str.strip().split()
    if len(parts) < 2:
        raise ValueError(f"Invalid build string format: {build_str}")

    num_jobs = 1
    x_idx = next((i for i, p in enumerate(parts) if p.lower().startswith('x')), -1)
    if x_idx >= 0:
        num_jobs = int(parts[x_idx][1:])
        parts = parts[:x_idx] + parts[x_idx+1:]

    first_num = next((i for i, p in enumerate(parts) if p.isdigit()), -1)
    if first_num <= 0:
        raise ValueError(f"Invalid build string format: {build_str}")

    item_name = ' '.join(parts[:first_num])
    tokens = parts[first_num:]

    if len(tokens) == 1:
        return BuildParams(item_name, int(tokens[0]), num_jobs)
    if len(tokens) == 2:
        try:
            return BuildParams(item_name, int(tokens[0]), num_jobs, me=int(tokens[1]))
        except ValueError:
            return BuildParams(item_name, int(tokens[0]), num_jobs, decryptor=tokens[1], build_type='invention')
    if len(tokens) == 3:
        return BuildParams(item_name, int(tokens[0]), num_jobs, me=int(tokens[1]), te=int(tokens[2]))
    if len(tokens) == 4:
        return BuildParams(item_name, int(tokens[0]), num_jobs, decryptor=tokens[1], relic=tokens[2], build_type='re')

    raise ValueError(f"Invalid build string format. Unexpected argument count: {build_str}")


def parse_stockpile_file(filepath: Path) -> dict:
    """Parse a stockpile file and return a dict of item_name -> quantity."""
    stockpile = {}
    if not filepath or not filepath.exists():
        return stockpile

    with open(filepath, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue

            parts = line.split()
            if len(parts) < 2:
                continue

            # Find the first numeric part (quantity)
            qty_idx = -1
            for i, part in enumerate(parts):
                if part.isdigit():
                    qty_idx = i
                    break

            if qty_idx <= 0:
                continue

            item_name = ' '.join(parts[:qty_idx])
            try:
                quantity = int(parts[qty_idx])
            except ValueError:
                continue

            stockpile[item_name] = stockpile.get(item_name, 0) + quantity

    return stockpile


def parse_targets_file(filepath: Path) -> dict:
    """Parse a targets file and return a dict of item_name -> target_quantity."""
    targets = {}
    if not filepath or not filepath.exists():
        return targets

    with open(filepath, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue

            parts = line.split()
            if len(parts) < 2:
                continue

            # Find the first numeric part (quantity)
            qty_idx = -1
            for i, part in enumerate(parts):
                if part.isdigit():
                    qty_idx = i
                    break

            if qty_idx <= 0:
                continue

            item_name = ' '.join(parts[:qty_idx])
            try:
                quantity = int(parts[qty_idx])
            except ValueError:
                continue

            targets[item_name] = targets.get(item_name, 0) + quantity

    return targets


def find_product_by_name(data: SDEData, name: str, bp_lookup: bool = True):
    for tid, tdata in data.types.items():
        if tdata['typeName'] == name:
            return tdata, tid, False
    if bp_lookup:
        for bp_name, bid in data.blueprint_names.items():
            if bp_name == name:
                return data.types.get(bid), bid, True
    return None, None, False


def resolve_item_tid(data: SDEData, name: str) -> int:
    """Find the type ID for an item by name. Returns None if not found."""
    tdata, tid, _ = find_product_by_name(data, name, bp_lookup=False)
    if tid:
        return tid
    # Try blueprint lookup
    _, bid, _ = find_product_by_name(data, name, bp_lookup=True)
    if bid:
        # For blueprint names, resolve to the product
        pid, _ = resolve_blueprint_product(data, bid)
        if pid:
            return pid
    return None


def resolve_blueprint_product(data: SDEData, bid: int):
    for pid, acts in data.products.items():
        if pid == bid:
            for act in acts.values():
                if act:
                    product_id = next(iter(act.keys()))
                    return product_id, data.types.get(product_id)
    return None, None


def generate_invention_report(data: SDEData, name: str, skill: int) -> str:
    tdata, tid, is_bp = find_product_by_name(data, name)
    if not tdata:
        return f"Error: Item '{name}' not found or not published.\n"

    if is_bp:
        inv = data.products.get(tid, {}).get(INVENTION, {})
        if inv:
            tid = next(iter(inv.keys()))
            tdata = data.types.get(tid)
        else:
            return f"Error: '{name}' is not an inventable blueprint.\n"

    inv = get_invention_data(data, tid, skill)
    if not inv:
        return f"Error: '{name}' is not an inventable item.\n"

    tech = inv['tech_level']
    if tech not in ('Tech II', 'Tech III'):
        return f"Error: '{name}' is not Tech II or Tech III.\n"

    cat, grp = get_group_path(data, tid)
    mg = get_market_group_path(data, tid)

    lines = [
        f"ITEM: {tdata['typeName']} ({tid})",
        f"Group: {cat} > {grp}",
        f"Market Group: {mg if mg else 'None'}",
        f"Tech Level: {tech}",
        "",
        "Invention:",
    ]

    if tech == 'Tech II':
        lines.append(f"Base: {_get_type_name(data, inv['invention_blueprint_id'])} ({inv['invention_blueprint_id']})")

    dc = sorted(inv['datacores'], key=lambda d: d['name'].lower())
    lines.append(f"Datacores: {', '.join(d['name'] for d in dc)}")
    lines.append(f"Required Per Run: {dc[0]['quantity'] if dc else 0}")

    sk = sorted(inv['skills'], key=lambda s: s['name'].lower())
    lines.append(f"Skills: {', '.join(s['name'] for s in sk)}")
    lines.append(f"Time: {inv['time_minutes']}")

    if tech == 'Tech II':
        base_bp = inv['invention_blueprint_id']
        base_runs = data.products.get(base_bp, {}).get(INVENTION, {}).get(tid, 10)
        lines.extend([
            "Probabilities:",
            "| Decryptor                       | Probability | Runs | ME | TE |",
            "|---------------------------------|-------------|------|----|----|"
        ])
        for dname in sorted(DECRYPTORS.keys(), key=str.lower):
            prob = calculate_probability(inv['base_probability'], skill, dname)
            _, dr, dme, dte = DECRYPTORS[dname]
            lines.append(f"| {dname:<31} | {prob:>9.2f} | {base_runs + dr:>4} | {2 + dme:>2} | {4 + dte:>2} |")
    else:
        lines.extend([
            "Probabilities:",
            "| Invented From                | Decryptor                       | Probability | Runs | ME | TE |",
            "|------------------------------|---------------------------------|-------------|------|----|----|"
        ])
        for rel in ('Intact', 'Malfunctioning', 'Wrecked'):
            for dname in sorted(DECRYPTORS.keys(), key=str.lower):
                prob = calculate_probability(TECHIII_RELICS[rel], skill, dname)
                _, dr, dme, dte = DECRYPTORS[dname]
                lines.append(f"| {rel:<28} | {dname:<31} | {prob:>9.2f} | {TECHIII_BASE_RUNS[rel] + dr:>4} | {2 + dme:>2} | {4 + dte:>2} |")

    return '\n'.join(lines) + '\n'


def write_jobs_csv(path: Path, name: str, runs: int, days: float, count: int):
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['name', 'runs', 'days', 'count'])
        w.writerow([name, runs, days, count])


def write_materials_csv(path: Path, mat_data: dict):
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['name', 'to_buy', 'buy_volume', 'start_amount', 'end_amount'])
        for name in sorted(mat_data.keys()):
            qty = mat_data[name]
            w.writerow([name, qty, round(qty / 10, 1), 0, 0])


def _get_component_me_te(data: SDEData, component_tid: int, config: dict):
    _, grp_name = get_group_path(data, component_tid)

    if 'Capital' in grp_name or 'Capital' in _get_type_name(data, component_tid):
        return (0, 0)

    component_defaults = config.get('component_bpo_defaults', {})
    me = component_defaults.get('me', 10)
    te = component_defaults.get('te', 20)
    return (me, te)


def _calculate_target_item_runs(data: SDEData, tid: int, target_qty: int, start_qty: int,
                                config: dict) -> TargetItem:
    """Calculate how many runs are needed to reach a target quantity for an item."""
    item_name = _get_type_name(data, tid) or f"Type {tid}"

    # Find if the item is buildable via manufacturing or reaction
    acts = data.products.get(tid, {})
    activity_id = MANUFACTURING if MANUFACTURING in acts else (REACTION if REACTION in acts else None)

    if activity_id:
        # Find the blueprint ID
        bid = None
        for pid, acts_check in data.products.items():
            if activity_id in acts_check and tid in acts_check[activity_id]:
                bid = pid
                break

        if bid:
            recipe = get_recipe(data, tid)
            if recipe:
                output_per_run = recipe['output_qty']
                needed = max(0, target_qty - start_qty)
                # With ME, materials reduce but output stays the same, so we don't apply ME to output
                runs_needed = (needed + output_per_run - 1) // output_per_run if output_per_run > 0 else 0
                # If we already have enough, don't need to build
                runs_needed = max(0, runs_needed)

                days_per_run = _get_days_per_run(data, bid, tid, activity_id, 0, config)
                return TargetItem(
                    name=item_name,
                    target_qty=target_qty,
                    start_qty=start_qty,
                    me=0,
                    te=0,
                    build_type='normal',
                    needs_to_build=runs_needed > 0,
                    runs_needed=runs_needed,
                    bid=bid,
                    tid=tid
                )

    # Check if inventable (T2/T3)
    inv = get_invention_data(data, tid, config.get('invention_skill', 3))
    if inv:
        skill_level = config.get('invention_skill', 3)
        tech = inv['tech_level']
        decryption_bonus = DECRYPTORS.get('None', (0, 0, 0, 0))
        # For T2: Use base runs from invention
        # For T3: Use base runs from relic type

        if tech == 'Tech II':
            base_bp_id = inv['invention_blueprint_id']
            base_runs = data.products.get(base_bp_id, {}).get(INVENTION, {}).get(tid, 10)
            # No decryptor used, so we use base runs directly
            needed = max(0, target_qty - start_qty)
            runs_needed = (needed + base_runs - 1) // base_runs if base_runs > 0 else 0

            days_per_run = _get_days_per_run(data, base_bp_id, tid, INVENTION, 0, config)
            return TargetItem(
                name=item_name,
                target_qty=target_qty,
                start_qty=start_qty,
                me=2,  # T2 ME is always 2 (no decryptor)
                te=4,  # T2 TE is always 4 (no decryptor)
                build_type='invention',
                needs_to_build=runs_needed > 0,
                runs_needed=runs_needed,
                bid=base_bp_id,
                tid=tid
            )
        elif tech == 'Tech III':
            # Use Malfunctioning relic as default for T3
            rel = 'Malfunctioning'
            base_runs = TECHIII_BASE_RUNS.get(rel, 10)
            needed = max(0, target_qty - start_qty)
            runs_needed = (needed + base_runs - 1) // base_runs if base_runs > 0 else 0

            days_per_run = _get_days_per_run(data, inv['invention_blueprint_id'], tid, INVENTION, 0, config)
            return TargetItem(
                name=item_name,
                target_qty=target_qty,
                start_qty=start_qty,
                me=2,
                te=4,
                build_type='re',
                needs_to_build=runs_needed > 0,
                runs_needed=runs_needed,
                bid=inv['invention_blueprint_id'],
                tid=tid
            )

    # Item is not buildable - needs to be bought
    return TargetItem(
        name=item_name,
        target_qty=target_qty,
        start_qty=start_qty,
        needs_to_build=False,
        runs_needed=0,
        tid=tid
    )


def _get_days_per_run(data: SDEData, bid: int, tid: int, activity_id: int, te: int, config: dict):
    bp_time = data.activities.get(bid, {}).get(activity_id, 0) or data.activities.get(tid, {}).get(activity_id, 0)
    te_bonus = config.get('te_bonus', 20)
    full_te = bp_time * (1 - te / 100) * (1 - te_bonus / 100)
    return full_te / 60 / 24


def _collect_all_buildable_items(data: SDEData, start_tid: int, start_me: int, start_te: int,
                                  config: dict, me_override=None, te_override=None):
    items = {}
    queue = [(start_tid, start_me, start_te)]
    visited = set()

    while queue:
        current_tid, current_me, current_te = queue.pop(0)

        if current_tid in visited:
            continue
        visited.add(current_tid)

        acts = data.products.get(current_tid, {})
        activity_id = MANUFACTURING if MANUFACTURING in acts else REACTION
        recipe = get_recipe(data, current_tid, me_override or current_me, activity_id)

        if not recipe:
            continue

        bid = None
        for pid, acts_check in data.products.items():
            if activity_id in acts_check and current_tid in acts_check[activity_id]:
                bid = pid
                break

        if not bid:
            continue

        days_per_run = _get_days_per_run(data, bid, current_tid, activity_id, current_te, config)

        items[current_tid] = {
            'total_runs': 0,
            'me': me_override if me_override is not None else current_me,
            'te': te_override if te_override is not None else current_te,
            'days_per_run': days_per_run,
            'name': _get_type_name(data, current_tid) or f'Type {current_tid}',
            'bid': bid,
            'recipe_qty': recipe['output_qty'],
            'material_tids': [m['typeID'] for m in recipe['materials']],
        }

        for mat in recipe['materials']:
            mat_tid = mat['typeID']
            if mat_tid in data.buildable_types and mat_tid not in visited and mat_tid not in items:
                comp_me, comp_te = _get_component_me_te(data, mat_tid, config)
                queue.append((mat_tid, comp_me, comp_te))

    return items


def _calculate_total_runs(items: dict, start_tid: int, runs_per_job: int, num_jobs: int) -> None:
    total_output_needed = runs_per_job * num_jobs

    dependency_map = defaultdict(set)
    for tid, item in items.items():
        for mat_tid in item['material_tids']:
            if mat_tid in items:
                dependency_map[mat_tid].add(tid)

    items[start_tid]['total_runs'] = total_output_needed

    queue = [start_tid]
    processed = set()

    while queue:
        current_tid = queue.pop(0)
        if current_tid in processed:
            continue
        processed.add(current_tid)

        if current_tid != start_tid:
            dependents = dependency_map.get(current_tid, set())
            total_runs = 0
            for dep_tid in dependents:
                if dep_tid not in items:
                    continue
                dep_item = items[dep_tid]

                recipe = get_recipe(items[current_tid], items[current_tid]['me']) or {}
                if recipe:
                    mat_qty = next((m['quantity'] for m in recipe['materials'] if m['typeID'] == current_tid), 0)
                    if mat_qty > 0:
                        runs_for_this = (dep_item['total_runs'] * mat_qty + dep_item['recipe_qty'] - 1) // dep_item['recipe_qty']
                    else:
                        runs_for_this = dep_item['total_runs']
                else:
                    runs_for_this = dep_item['total_runs']

                total_runs += runs_for_this

            items[current_tid]['total_runs'] = total_runs

        for dep_tid in dependency_map.get(current_tid, set()):
            if dep_tid not in processed:
                queue.append(dep_tid)


def _generate_plan_with_targets(data: SDEData, params: BuildParams, config: dict,
                                stockpile: dict, targets: dict, out_dir: Path) -> str:
    """Generate a comprehensive plan including stock targets."""
    out_dir.mkdir(parents=True, exist_ok=True)

    # Process stockpile and resolve type IDs
    stockpile_tids = {}
    for name, qty in stockpile.items():
        tid = resolve_item_tid(data, name)
        if tid:
            stockpile_tids[tid] = qty

    # Process targets and resolve type IDs
    target_tids = {}
    for name, target_qty in targets.items():
        tid = resolve_item_tid(data, name)
        if tid:
            start_qty = stockpile_tids.get(tid, 0)
            target_item = _calculate_target_item_runs(data, tid, target_qty, start_qty, config)
            target_tids[tid] = target_item

    # Collect all buildable items from main build (if any)
    all_items = {}
    main_tid = None

    if params.item_name:
        tdata, tid, found_bp = find_product_by_name(data, params.item_name)
        if not tdata:
            return f"Error: Item '{params.item_name}' not found or not published.\n"

        bid = tid if found_bp else None
        if bid:
            tid, tdata = resolve_blueprint_product(data, bid) or (tid, tdata)
            if not tid:
                return "Error: No product found for blueprint.\n"

        main_tid = tid

        # Collect all buildable items for the main build
        me_override = params.me if params.build_all else None
        te_override = params.te if params.build_all else None
        all_items = _collect_all_buildable_items(data, tid, params.me, params.te, config,
                                                  me_override, te_override)

    # Add target items that need to be built
    for tid, target_item in target_tids.items():
        if not target_item.needs_to_build:
            continue

        if tid in all_items:
            # Item already exists - add the target runs to it
            existing = all_items[tid]
            existing['target_runs'] = existing.get('target_runs', 0) + target_item.runs_needed
            # Adjust total runs if needed
            existing['total_runs'] = max(existing.get('total_runs', 0), existing.get('target_runs', 0))
        else:
            # Add new item
            acts = data.products.get(tid, {})
            activity_id = MANUFACTURING if MANUFACTURING in acts else REACTION

            bid = target_item.bid
            if not bid:
                continue

            recipe = get_recipe(data, tid, target_item.me) if activity_id else None
            if not recipe:
                continue

            days_per_run = _get_days_per_run(data, bid, tid, activity_id, target_item.te, config)

            all_items[tid] = {
                'total_runs': target_item.runs_needed,
                'target_runs': target_item.runs_needed,
                'me': target_item.me,
                'te': target_item.te,
                'days_per_run': days_per_run,
                'name': target_item.name,
                'bid': bid,
                'recipe_qty': recipe['output_qty'],
                'material_tids': [m['typeID'] for m in recipe['materials']],
                'is_target_item': True
            }

    # Ensure main_tid entry exists in all_items
    # If Barrage L is not buildable, we still need to track its materials
    if not all_items:
        # Try to find the blueprint and product
        tdata, tid_found, found_bp = find_product_by_name(data, params.item_name)
        if tid_found:
            acts = data.products.get(tid_found, {})
            activity_id = MANUFACTURING if MANUFACTURING in acts else (REACTION if REACTION in acts else None)
            if activity_id:
                bid = None
                for pid, acts_check in data.products.items():
                    if activity_id in acts_check and tid_found in acts_check[activity_id]:
                        bid = pid
                        break
                if bid:
                    recipe = get_recipe(data, tid_found)
                    if recipe:
                        days_per_run = _get_days_per_run(data, bid, tid_found, activity_id, 0, config)
                        all_items[tid_found] = {
                            'total_runs': params.runs_per_job * params.num_jobs,
                            'me': params.me,
                            'te': params.te,
                            'days_per_run': days_per_run,
                            'name': params.item_name,
                            'bid': bid,
                            'recipe_qty': recipe['output_qty'],
                            'material_tids': [m['typeID'] for m in recipe['materials']],
                        }

    if main_tid and main_tid not in all_items:
        # This happens when main item is not buildable but we still want to track it
        tdata, tid_found, found_bp = find_product_by_name(data, params.item_name)
        if tid_found:
            acts = data.products.get(tid_found, {})
            activity_id = MANUFACTURING if MANUFACTURING in acts else (REACTION if REACTION in acts else None)
            if activity_id:
                bid = None
                for pid, acts_check in data.products.items():
                    if activity_id in acts_check and tid_found in acts_check[activity_id]:
                        bid = pid
                        break
                if bid:
                    recipe = get_recipe(data, tid_found)
                    if recipe:
                        days_per_run = _get_days_per_run(data, bid, tid_found, activity_id, 0, config)
                        all_items[tid_found] = {
                            'total_runs': params.runs_per_job * params.num_jobs,
                            'me': params.me,
                            'te': params.te,
                            'days_per_run': days_per_run,
                            'name': params.item_name,
                            'bid': bid,
                            'recipe_qty': recipe['output_qty'],
                            'material_tids': [m['typeID'] for m in recipe['materials']],
                        }

    # Recalculate total runs for dependency tree (to ensure all materials are covered)
    if main_tid and params.build_all and main_tid in all_items:
        _calculate_total_runs(all_items, main_tid, params.runs_per_job, params.num_jobs)

    # Generate materials CSV with stockpile consideration
    mat_total = defaultdict(int)
    bought_mats = defaultdict(int)

    # Track end amounts for target items
    target_end_amounts = {}
    for tid, target_item in target_tids.items():
        target_end_amounts[tid] = target_item.target_qty

    for current_tid, item in all_items.items():
        recipe = get_recipe(data, current_tid, item['me'])
        if not recipe:
            continue

        total_runs = item['total_runs']

        for mat in recipe['materials']:
            mat_tid = mat['typeID']
            mat_name = _get_type_name(data, mat_tid) or f'Type {mat_tid}'

            if mat_tid in data.buildable_types:
                continue

            # Check if we have stockpile for this material
            start_qty = stockpile_tids.get(mat_tid, 0)

            # Calculate needed quantity with ME
            mat_qty = math.ceil(max(1, mat['quantity'] * (1 - item['me'] / 100)))
            total_needed = mat_qty * total_runs

            # Subtract stockpile
            available_from_stock = min(start_qty, total_needed)
            to_buy = total_needed - available_from_stock
            mat_total[mat_name] += to_buy
            bought_mats[mat_name] += to_buy

            # Update stockpile after usage
            stockpile_tids[mat_tid] = start_qty - available_from_stock

    # Calculate end amounts for target items
    for tid, target_item in target_tids.items():
        # Items that need to be built will be produced, so track end amount
        if target_item.needs_to_build:
            start = target_item.start_qty
            produced = target_item.runs_needed
            # Calculate actual production based on blueprint output
            acts = data.products.get(tid, {})
            activity_id = MANUFACTURING if MANUFACTURING in acts else (REACTION if REACTION in acts else None)
            if activity_id:
                recipe = get_recipe(data, tid)
                if recipe:
                    output_per_run = recipe['output_qty']
                    actual_production = produced * output_per_run
                else:
                    actual_production = produced
            else:
                actual_production = produced

            end_amount = start + actual_production
            # Cap at target
            end_amount = min(end_amount, target_item.target_qty)
        else:
            # Needs to be bought - no production
            end_amount = target_item.start_qty

        # Track stock changes for materials that might be affected by production
        pass  # Stockpile materials are already handled above

    # Write materials CSV
    materials_data = {}
    for name, to_buy in bought_mats.items():
        materials_data[name] = to_buy

    # Add stockpile items to materials.csv with their changes
    for tid, start_qty in stockpile_tids.items():
        tid_for_target = None
        for t, target_item in target_tids.items():
            if t == tid:
                tid_for_target = t
                break

        if tid_for_target is not None:
            target_item = target_tids[tid_for_target]
            start = target_item.start_qty
            # Calculate end from production
            if target_item.needs_to_build:
                produced = target_item.runs_needed
                acts = data.products.get(tid, {})
                activity_id = MANUFACTURING if MANUFACTURING in acts else (REACTION if REACTION in acts else None)
                if activity_id:
                    recipe = get_recipe(data, tid)
                    output_per_run = recipe['output_qty'] if recipe else 1
                    actual_production = produced * output_per_run
                else:
                    actual_production = produced
                end = min(start + actual_production, target_item.target_qty)
            else:
                end = start

            materials_data[target_item.name] = bought_mats.get(target_item.name, 0)

    # Rewrite materials data with correct format: to_buy, buy_volume, start_amount, end_amount
    final_materials = []
    all_item_names_set = set(materials_data.keys())

    # Get all materials from build tree
    for current_tid, item in all_items.items():
        recipe = get_recipe(data, current_tid, item['me'])
        if not recipe:
            continue

        for mat in recipe['materials']:
            mat_name = _get_type_name(data, mat['typeID']) or f'Type {mat["typeID"]}'
            if mat_name not in materials_data:
                final_materials.append({
                    'name': mat_name,
                    'to_buy': 0,
                    'start_amount': 0,
                    'end_amount': 0
                })

    for name, to_buy in materials_data.items():
        found = False
        for m in final_materials:
            if m['name'] == name:
                m['to_buy'] = to_buy
                found = True
                break
        if not found:
            final_materials.append({
                'name': name,
                'to_buy': to_buy,
                'start_amount': 0,
                'end_amount': 0
            })

    # Set start/end amounts for target items
    for tid, target_item in target_tids.items():
        for m in final_materials:
            if m['name'] == target_item.name:
                m['start_amount'] = target_item.start_qty
                m['end_amount'] = target_end_amounts[tid]
                break

    # Write CSV
    with open(out_dir / 'materials.csv', 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['name', 'to_buy', 'buy_volume', 'start_amount', 'end_amount'])
        for m in sorted(final_materials, key=lambda x: x['name']):
            w.writerow([m['name'], m['to_buy'], round(m['to_buy'] / 10, 1), m['start_amount'], m['end_amount']])

    # Generate jobs CSV
    split_days = float(config.get('split_days', 3.0))
    if split_days > 30:
        split_days = 3.0

    jobs_list = []

    # Add main build jobs if present
    if main_tid and main_tid in all_items:
        main_item = all_items[main_tid]
        if main_item['total_runs'] <= 0 and params.item_name:
            main_item['total_runs'] = params.runs_per_job * params.num_jobs

        if main_item['total_runs'] > 0:
            job_splits = split_job_runs(main_item['total_runs'], split_days, main_item['days_per_run'])
            for runs, count in job_splits:
                days = round(main_item['days_per_run'] * runs, 2)
                bp_name = _get_type_name(data, main_item['bid']) or f"Blueprint {main_item['bid']}"
                jobs_list.append({
                    'name': bp_name,
                    'runs': runs,
                    'days': days,
                    'count': count,
                })

    # Add target item jobs
    for tid, target_item in target_tids.items():
        if not target_item.needs_to_build:
            continue

        if tid in all_items:
            item = all_items[tid]
            runs_needed = item.get('target_runs', target_item.runs_needed)
            if runs_needed <= 0:
                continue

            job_splits = split_job_runs(runs_needed, split_days, item['days_per_run'])
            for runs, count in job_splits:
                days = round(item['days_per_run'] * runs, 2)
                bp_name = _get_type_name(data, item['bid']) or f"Blueprint {item['bid']}"
                jobs_list.append({
                    'name': bp_name,
                    'runs': runs,
                    'days': days,
                    'count': count,
                })
        else:
            # Add target directly
            if target_item.runs_needed > 0:
                acts = data.products.get(tid, {})
                activity_id = MANUFACTURING if MANUFACTURING in acts else (REACTION if REACTION in acts else None)
                if activity_id and target_item.bid:
                    days_per_run = _get_days_per_run(data, target_item.bid, tid, activity_id, target_item.te, config)
                    job_splits = split_job_runs(target_item.runs_needed, split_days, days_per_run)
                    for runs, count in job_splits:
                        days = round(days_per_run * runs, 2)
                        bp_name = _get_type_name(data, target_item.bid) or f"Blueprint {target_item.bid}"
                        jobs_list.append({
                            'name': bp_name,
                            'runs': runs,
                            'days': days,
                            'count': count,
                        })

    jobs_list.sort(key=lambda x: (x['days'], x.get('me', 0), x.get('te', 0)))

    if main_tid:
        bp_name = _get_type_name(data, tid) or f"Blueprint {tid}"
        with open(out_dir / 'jobs.csv', 'w', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            w.writerow(['name', 'runs', 'days', 'count'])
            for job in jobs_list:
                w.writerow([job['name'], job['runs'], job['days'], job['count']])

    return f"Plan generated: {out_dir / 'materials.csv'}, {out_dir / 'jobs.csv'}\n"



def _gen_invention_plan(data: SDEData, params: BuildParams, config: dict, out_dir: Path, tid: int, bid: int):
    inv = get_invention_data(data, tid, config.get('invention_skill', 3))
    if not inv:
        return "Error: item not inventable.\n"

    prob = calculate_probability(inv['base_probability'], config.get('invention_skill', 3), params.decryptor)
    _, dr, _, _ = DECRYPTORS.get(params.decryptor, (0, 0, 0, 0))

    rel = params.relic or 'Malfunctioning'
    base_runs = TECHIII_BASE_RUNS.get(rel, 10) if params.build_type == 're' else \
                data.products.get(inv['invention_blueprint_id'], {}).get(INVENTION, {}).get(tid, 10)
    runs = base_runs + dr

    total_runs = params.num_jobs * params.runs_per_job
    exp_bp_runs = math.ceil(total_runs / (runs * prob))

    dc_materials = data.materials.get(inv['invention_blueprint_id'], {}).get(INVENTION, [])
    dc_data = {}
    for mat in dc_materials:
        mname = _get_type_name(data, mat['typeID'])
        if mname and mname.startswith('Datacore - '):
            dc_data[mname] = dc_data.get(mname, 0) + mat['quantity'] * exp_bp_runs

    write_materials_csv(out_dir / 'materials.csv', dc_data)

    invent_time = inv['time_minutes'] * exp_bp_runs
    bp_name = _get_type_name(data, inv['invention_blueprint_id']) or f"Blueprint {inv['invention_blueprint_id']}"
    write_jobs_csv(out_dir / 'jobs.csv', f"{bp_name} Blueprints", exp_bp_runs, math.ceil(invent_time / 60 / 24 * 100) / 100, 1)

    return f"Plan generated: {out_dir / 'materials.csv'}, {out_dir / 'jobs.csv'}\n"


def _gen_manufacturing_plan(data: SDEData, params: BuildParams, config: dict, out_dir: Path, tid: int, bid: int):
    if not bid:
        for pid, acts in data.products.items():
            if MANUFACTURING in acts and tid in acts[MANUFACTURING]:
                bid = pid
                break
        if not bid:
            return "Error: No manufacturing blueprint found.\n"

    if params.build_all:
        return _gen_manufacturing_plan_build_all(data, params, config, out_dir, tid, bid)

    run_days = _get_days_per_run(data, bid, tid, MANUFACTURING, params.te, config)

    materials = [
        {'typeID': m['typeID'],
         'quantity': math.ceil(max(1, m['quantity'] * (1 - params.me / 100))),
         'original_quantity': m['quantity']}
        for m in data.materials.get(bid, {}).get(MANUFACTURING, [])
    ]

    total_runs = params.num_jobs * params.runs_per_job
    mat_total = {}
    for mat in materials:
        name = _get_type_name(data, mat['typeID']) or f'Type {mat["typeID"]}'
        mat_total[name] = mat_total.get(name, 0) + mat['quantity'] * total_runs

    write_materials_csv(out_dir / 'materials.csv', mat_total)

    bp_name = _get_type_name(data, bid) or f"Blueprint {bid}"
    with open(out_dir / 'jobs.csv', 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['name', 'runs', 'days', 'count'])
        for j in range(1, params.num_jobs + 1):
            w.writerow([bp_name, params.runs_per_job, round(run_days * params.runs_per_job, 2), j])

    return f"Plan generated: {out_dir / 'materials.csv'}, {out_dir / 'jobs.csv'}\n"


def _gen_manufacturing_plan_build_all(data: SDEData, params: BuildParams, config: dict, out_dir: Path, tid: int, bid: int):
    split_days = float(config.get('split_days', 3.0))
    if split_days > 30:
        split_days = 3.0

    items = _collect_all_buildable_items(data, tid, params.me, params.te, config,
                                          params.me if params.build_all else None,
                                          params.te if params.build_all else None)

    if not items:
        return "Error: No buildable items found in dependency tree.\n"

    _calculate_total_runs(items, tid, params.runs_per_job, params.num_jobs)

    mat_total = defaultdict(int)

    for current_tid, item in items.items():
        recipe = get_recipe(data, current_tid, item['me'])
        if not recipe:
            continue

        total_runs = item['total_runs']

        for mat in recipe['materials']:
            mat_tid = mat['typeID']
            mat_name = _get_type_name(data, mat_tid) or f'Type {mat_tid}'

            if mat_tid in data.buildable_types:
                continue

            mat_qty = math.ceil(max(1, mat['quantity'] * (1 - item['me'] / 100)))
            mat_total[mat_name] += mat_qty * total_runs

    write_materials_csv(out_dir / 'materials.csv', dict(mat_total))

    jobs_list = []

    for current_tid, item in items.items():
        if item['total_runs'] <= 0:
            continue

        recipe = get_recipe(data, current_tid, item['me'])
        if not recipe:
            continue

        job_splits = split_job_runs(item['total_runs'], split_days, item['days_per_run'])

        for runs, count in job_splits:
            days = round(item['days_per_run'] * runs, 2)
            jobs_list.append({
                'name': item['name'],
                'runs': runs,
                'days': days,
                'count': count,
            })

    jobs_list.sort(key=lambda x: (x['days'], x.get('me', 0), x.get('te', 0)))

    bp_name = _get_type_name(data, tid) or f"Blueprint {tid}"
    with open(out_dir / 'jobs.csv', 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['name', 'runs', 'days', 'count'])
        for job in jobs_list:
            w.writerow([job['name'], job['runs'], job['days'], job['count']])

    return f"Plan generated: {out_dir / 'materials.csv'}, {out_dir / 'jobs.csv'}\n"


def generate_plan(data: SDEData, params: BuildParams, config: dict, out_dir: Path,
                  build_all: bool = False, stockpile: dict = None, targets: dict = None) -> str:
    params.build_all = build_all
    out_dir.mkdir(parents=True, exist_ok=True)

    # If stockpile or targets are provided, use the new comprehensive plan generator
    if stockpile is not None or targets is not None:
        stockpile = stockpile or {}
        targets = targets or {}
        return _generate_plan_with_targets(data, params, config, stockpile, targets, out_dir)

    # Existing behavior for no stockpile/targets
    tdata, tid, found_bp = find_product_by_name(data, params.item_name)
    if not tdata:
        return f"Error: Item '{params.item_name}' not found or not published.\n"

    bid = tid if found_bp else None
    if bid:
        tid, tdata = resolve_blueprint_product(data, bid) or (tid, tdata)
        if not tid:
            return "Error: No product found for blueprint.\n"

    if params.build_type in ('invention', 're'):
        return _gen_invention_plan(data, params, config, out_dir, tid, bid)
    return _gen_manufacturing_plan(data, params, config, out_dir, tid, bid)


def generate_report(data: SDEData, name: str, me: int = 0, te: int = 0, waste: bool = False) -> str:
    tdata, tid, found_bp = find_product_by_name(data, name)
    if not tdata:
        return f"Error: Item '{name}' not found or not published.\n"

    bid = tid if found_bp else None
    disp_tid, disp_tdata = tid, tdata
    if bid:
        disp_tid, disp_tdata = resolve_blueprint_product(data, bid) or (tid, tdata)

    recipe = get_recipe(data, disp_tid, me)
    if not recipe:
        return f"Error: No industry activity found for '{name}'.\n"

    cat, grp = get_group_path(data, disp_tid)
    mg = get_market_group_path(data, disp_tid)
    tech = get_tech_level(data, disp_tid)
    vol = get_volume(data, disp_tid)

    run_time = round(recipe['run_time'] * (1 - te / 100)) if te > 0 else recipe['run_time']

    def mat_row(m):
        mname = _get_type_name(data, m['typeID']) or 'Unknown'
        buildable = 'Yes' if any(a in data.buildable_types for a in [m['typeID']]) else 'No'
        if waste:
            return f"| {mname} | {m['quantity']} | {m['original_quantity'] - m['quantity']} | {buildable} |"
        return f"| {mname} | {m['quantity']} | {buildable} |"

    header = "| Item | Quantity | Waste | Buildable |" if waste else "| Item | Quantity | Buildable |"
    sep = "|:-:|:-:|:-:|---:|" if waste else "|:-:|:---:|---:|"

    lines = [
        f"ITEM: {disp_tdata['typeName']} ({disp_tid})",
        f"Group: {cat} > {grp}",
        f"Market Group: {mg if mg else 'None'}",
        f"Tech Level: {tech}",
        f"Volume: {vol:.2f}",
        "",
        "Recipe:",
        f"Activity: {recipe['activity']}",
        f"Output Quantity: {recipe['output_qty']}",
        f"Run Time: {run_time}",
        header,
        sep,
    ]
    for mat in sorted(recipe['materials'], key=lambda m: (_get_type_name(data, m['typeID']) or '').lower()):
        lines.append(mat_row(mat))

    return '\n'.join(lines) + '\n'


def main():
    p = argparse.ArgumentParser(description='EVE Online Industrial Planner')
    s = p.add_subparsers(dest='command')

    r = s.add_parser('recipe', help='Generate recipe report')
    r.add_argument('target')
    r.add_argument('--sde', required=True)
    r.add_argument('--me', type=int, default=0)
    r.add_argument('--te', type=int, default=0)
    r.add_argument('--waste', action='store_true')

    inv = s.add_parser('invention', help='Generate invention report')
    inv.add_argument('target')
    inv.add_argument('--sde', required=True)
    inv.add_argument('--skill', type=int, choices=range(1, 6), default=5)

    pl = s.add_parser('plan', help='Create build plan')
    pl.add_argument('build_string')
    pl.add_argument('--config', required=True)
    pl.add_argument('--sde', required=True)
    pl.add_argument('--output', required=True)
    pl.add_argument('--build-all', action='store_true', help='Build all components in the dependency tree')
    pl.add_argument('--stocks', help='Path to file with current stockpile items')
    pl.add_argument('--targets', help='Path to file with stock target quantities')

    args = p.parse_args()
    if not args.command:
        p.print_help()
        sys.exit(1)

    sde_path = Path(args.sde)
    if not sde_path.exists():
        print(f"Error: SDE '{args.sde}' not found.", file=sys.stderr)
        sys.exit(1)

    d = parse_sde(sde_path)

    if args.command == 'recipe':
        if not 0 <= args.me <= 10:
            print(f"Error: ME must be 0-10, got {args.me}", file=sys.stderr)
            sys.exit(1)
        if not 0 <= args.te <= 20 or args.te % 2 != 0:
            print(f"Error: TE must be even 0-20, got {args.te}", file=sys.stderr)
            sys.exit(1)
        print(generate_report(d, args.target, args.me, args.te, args.waste), end='')
    elif args.command == 'invention':
        print(generate_invention_report(d, args.target, args.skill), end='')
    elif args.command == 'plan':
        try:
            params = parse_build_string(args.build_string)
            params.build_all = args.build_all
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        cfg = load_yaml_file(Path(args.config))

        stockpile = {}
        if args.stocks:
            stockpile = parse_stockpile_file(Path(args.stocks))

        targets = {}
        if args.targets:
            targets = parse_targets_file(Path(args.targets))

        print(generate_plan(d, params, cfg, Path(args.output), params.build_all, stockpile, targets), end='')


if __name__ == '__main__':
    main()
