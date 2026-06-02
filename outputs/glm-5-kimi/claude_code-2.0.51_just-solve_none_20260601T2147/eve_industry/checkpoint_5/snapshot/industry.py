#!/usr/bin/env python3
"""EVE Online Industry Recipe Planner.

Parses the EVE Online Static Data Export (SDE) and outputs a deterministic
recipe report for a target product or blueprint.
"""

import argparse
import bz2
import csv
import math
import sys
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import yaml

_TECH_LEVELS = {'Tech I', 'Tech II', 'Tech III'}
_BLUEPRINT_CATEGORY_ID = '9'
_SHIP_CATEGORY_ID = '6'
_MANUFACTURING = '1'
_INVENTION = '8'
_REACTION = '11'
_SECONDS_PER_DAY = 86400.0

DECRYPTORS = [
    {'name': 'None',                            'prob_mult': 0,   'runs': 0,  'me': 0,  'te': 0},
    {'name': 'Accelerant Decryptor',            'prob_mult': 20,  'runs': 1,  'me': 2,  'te': 10},
    {'name': 'Attainment Decryptor',            'prob_mult': 80,  'runs': 4,  'me': -1, 'te': 4},
    {'name': 'Augmentation Decryptor',          'prob_mult': -40, 'runs': 9,  'me': -2, 'te': 2},
    {'name': 'Optimized Attainment Decryptor',  'prob_mult': 90,  'runs': 2,  'me': 1,  'te': -2},
    {'name': 'Optimized Augmentation Decryptor','prob_mult': -10, 'runs': 7,  'me': 2,  'te': 0},
    {'name': 'Parity Decryptor',                'prob_mult': 50,  'runs': 3,  'me': 1,  'te': -2},
    {'name': 'Process Decryptor',               'prob_mult': 10,  'runs': 0,  'me': 3,  'te': 6},
    {'name': 'Symmetry Decryptor',              'prob_mult': 0,   'runs': 2,  'me': 1,  'te': 8},
]

RELIC_QUALITY_ORDER = ['Intact', 'Malfunctioning', 'Wrecked']


class SDEError(Exception):
    pass


# ── SDE loading ───────────────────────────────────────────────────

class SDE:
    def __init__(self, sde_dir):
        p = Path(sde_dir)

        def bz2_csv(name):
            with bz2.open(p / name, 'rt', encoding='utf-8') as f:
                return list(csv.DictReader(f))

        def index(rows, key):
            return {row[key]: row for row in rows}

        def group(rows, *keys):
            out = defaultdict(list)
            for row in rows:
                out[tuple(row[k] for k in keys)].append(row)
            return dict(out)

        inv_types = bz2_csv('invTypes.csv.bz2')

        self.types = index(inv_types, 'typeID')
        self.types_by_name = {t['typeName']: t for t in inv_types}
        self.groups = index(bz2_csv('invGroups.csv.bz2'), 'groupID')
        self.categories = index(bz2_csv('invCategories.csv.bz2'), 'categoryID')
        self.market_groups = index(bz2_csv('invMarketGroups.csv.bz2'), 'marketGroupID')
        self.meta_types = index(bz2_csv('invMetaTypes.csv.bz2'), 'typeID')
        self.meta_groups = index(bz2_csv('invMetaGroups.csv.bz2'), 'metaGroupID')

        self.products = group(bz2_csv('industryActivityProducts.csv.bz2'), 'typeID', 'activityID')
        self.materials = group(bz2_csv('industryActivityMaterials.csv.bz2'), 'typeID', 'activityID')
        self.skills = group(bz2_csv('industryActivitySkills.csv.bz2'), 'typeID', 'activityID')
        self.probabilities = group(bz2_csv('industryActivityProbabilities.csv.bz2'), 'typeID', 'activityID')

        self.time = {
            (a['typeID'], a['activityID']): int(a['time'])
            for a in bz2_csv('industryActivity.csv.bz2')
        }
        self.activity_names = {
            a['activityID']: a['activityName']
            for a in bz2_csv('ramActivities.csv.bz2')
        }

        with open(p / 'ship_volumes.yaml', 'r', encoding='utf-8') as f:
            self.ship_volumes = {k: v for k, v in yaml.safe_load(f).items() if v is not None}

        self.product_to_bp = {}
        self.product_to_all_bps = defaultdict(list)
        self.buildable = set()
        for (bp_id, act_id), prods in self.products.items():
            for pr in prods:
                pid = pr['productTypeID']
                self.product_to_bp[pid] = (bp_id, act_id)
                self.product_to_all_bps[pid].append((bp_id, act_id))
                self.buildable.add(pid)

    # ── type lookups ──────────────────────────────────────────────

    def type_name(self, type_id):
        info = self.types.get(type_id)
        return info['typeName'] if info else 'Unknown'

    def bp_name(self, bp_id):
        info = self.types.get(bp_id)
        return info['typeName'] if info else self.type_name(bp_id)

    def is_blueprint(self, type_info):
        grp = self.groups.get(type_info['groupID'])
        return grp is not None and grp['categoryID'] == _BLUEPRINT_CATEGORY_ID

    def market_group_path(self, mg_id):
        if not mg_id or mg_id not in self.market_groups:
            return None
        parts = []
        cur = mg_id
        while cur and cur in self.market_groups and cur != 'None':
            parts.append(self.market_groups[cur]['marketGroupName'])
            cur = self.market_groups[cur]['parentGroupID']
        return ' > '.join(reversed(parts))

    def tech_level(self, type_id):
        mt = self.meta_types.get(type_id)
        if not mt:
            return 'Tech I'
        mg = self.meta_groups.get(mt['metaGroupID'])
        name = mg['metaGroupName'] if mg else ''
        return name if name in _TECH_LEVELS else 'Tech I'

    def volume(self, type_info):
        grp = self.groups.get(type_info['groupID'], {})
        cat = self.categories.get(grp.get('categoryID'))
        if cat and cat['categoryID'] == _SHIP_CATEGORY_ID:
            grp_name = grp.get('groupName', '')
            if grp_name in self.ship_volumes:
                return self.ship_volumes[grp_name]
        vol = type_info.get('volume', '0')
        if vol and vol != 'None':
            try:
                return float(vol)
            except ValueError:
                pass
        return 0.0

    def display_context(self, type_info):
        grp = self.groups.get(type_info['groupID'])
        cat = self.categories.get(grp['categoryID']) if grp else None
        return {
            'group_name': grp['groupName'] if grp else 'Unknown',
            'category_name': cat['categoryName'] if cat else 'Unknown',
            'market_group_path': self.market_group_path(type_info.get('marketGroupID')),
            'tech_level': self.tech_level(type_info['typeID']),
        }

    def named_materials(self, bp_id, activity_id):
        return sorted(
            [
                {'name': self.types[m['materialTypeID']]['typeName'],
                 'quantity': int(m['quantity'])}
                for m in self.materials.get((bp_id, activity_id), [])
                if m['materialTypeID'] in self.types
            ],
            key=lambda x: x['name'].lower(),
        )

    def named_skills(self, bp_id, activity_id):
        return sorted(
            [self.types[s['skillID']]['typeName']
             for s in self.skills.get((bp_id, activity_id), [])
             if s['skillID'] in self.types],
            key=str.lower,
        )


# ── target resolution ────────────────────────────────────────────

def _resolve_target(target_name, sde):
    type_info = sde.types_by_name.get(target_name)
    if not type_info:
        raise SDEError(f"'{target_name}' not found in SDE")
    if type_info['published'] != '1':
        raise SDEError(f"'{target_name}' is not published")

    type_id = type_info['typeID']

    if sde.is_blueprint(type_info):
        bp_id = type_id
        products = sde.products.get((bp_id, _MANUFACTURING))
        activity_id = _MANUFACTURING
        if not products:
            products = sde.products.get((bp_id, _REACTION))
            activity_id = _REACTION
        if not products:
            raise SDEError(
                f"Blueprint '{target_name}' has no manufacturing or reaction products"
            )

        product = products[0]
        product_type_id = product['productTypeID']
        display_info = sde.types.get(product_type_id)
        if not display_info:
            raise SDEError(f"Product type ID {product_type_id} not found")
        output_quantity = int(product['quantity'])
    else:
        product_type_id = type_id
        result = sde.product_to_bp.get(product_type_id)
        if not result:
            raise SDEError(f"No blueprint found for '{target_name}'")
        bp_id, activity_id = result
        products = sde.products.get((bp_id, activity_id))
        output_quantity = int(products[0]['quantity'])
        display_info = type_info

    return {
        'type_info': type_info,
        'type_id': type_id,
        'blueprint_type_id': bp_id,
        'activity_id': activity_id,
        'product_type_id': product_type_id,
        'output_quantity': output_quantity,
        'display_info': display_info,
    }


# ── recipe ───────────────────────────────────────────────────────

def get_recipe(target_name, sde, me=0, te=0, display_waste=False):
    resolved = _resolve_target(target_name, sde)

    bp_id = resolved['blueprint_type_id']
    activity_id = resolved['activity_id']
    display_info = resolved['display_info']

    ctx = sde.display_context(display_info)
    raw_mats = sde.materials.get((bp_id, activity_id), [])
    time_seconds = sde.time.get((bp_id, activity_id), 0)
    activity_name = sde.activity_names.get(activity_id, 'Unknown')

    time_minutes = math.ceil(time_seconds * (1 - te / 100) / 60)
    apply_me = activity_id == _MANUFACTURING

    materials = []
    for m in raw_mats:
        mat_info = sde.types.get(m['materialTypeID'])
        if not mat_info:
            continue
        base = int(m['quantity'])
        adj = math.ceil(max(1, base * (1 - me / 100))) if apply_me and me > 0 else base
        waste = base - adj if display_waste and apply_me else None
        materials.append({
            'name': mat_info['typeName'],
            'quantity': adj,
            'base_quantity': base,
            'waste': waste,
            'buildable': m['materialTypeID'] in sde.buildable,
        })
    materials.sort(key=lambda x: x['name'].lower())

    return {
        'type_id': display_info['typeID'],
        'type_name': display_info['typeName'],
        'category_name': ctx['category_name'],
        'group_name': ctx['group_name'],
        'market_group_path': ctx['market_group_path'],
        'tech_level': ctx['tech_level'],
        'volume': sde.volume(display_info),
        'activity_name': activity_name,
        'output_quantity': resolved['output_quantity'],
        'run_time': time_minutes,
        'materials': materials,
        'display_waste': display_waste,
    }


def format_recipe(recipe):
    vol = recipe['volume']
    vol_str = f"{int(vol)}.00" if vol == int(vol) else f"{vol:.2f}"
    lines = [
        f"ITEM: {recipe['type_name']} ({recipe['type_id']})",
        f"Group: {recipe['category_name']} > {recipe['group_name']}",
        f"Market Group: {recipe['market_group_path'] or 'None'}",
        f"Tech Level: {recipe['tech_level']}",
        f"Volume: {vol_str}",
        "",
        "Recipe:",
        f"Activity: {recipe['activity_name']}",
        f"Output Quantity: {recipe['output_quantity']}",
        f"Run Time: {recipe['run_time']}",
    ]

    if recipe.get('display_waste'):
        lines.append("| Item | Quantity |Waste   | Buildable |")
        lines.append("|:-:|:---:|:-----:|---:|")
        for mat in recipe['materials']:
            waste_str = str(mat['waste']) if mat['waste'] is not None else ''
            lines.append(
                f"| {mat['name']} | {mat['quantity']} | {waste_str}"
                f"| {'Yes' if mat['buildable'] else 'No'} |"
            )
    else:
        lines.append("| Item | Quantity | Buildable |")
        lines.append("|:-:|:---:|---:|")
        for mat in recipe['materials']:
            lines.append(
                f"| {mat['name']} | {mat['quantity']} | "
                f"{'Yes' if mat['buildable'] else 'No'} |"
            )

    return '\n'.join(lines)


# ── invention ────────────────────────────────────────────────────

def _invention_prob(base_prob, skill_level, science_count, prob_mult):
    base = Decimal(str(base_prob))
    sl = Decimal(str(skill_level))
    sc = Decimal(str(science_count))
    pm = Decimal(str(prob_mult))

    skill_factor = Decimal(1) + sc * sl / Decimal(30) + sl / Decimal(40)
    decryptor_factor = Decimal(1) + pm / Decimal(100)
    prob = base * skill_factor * decryptor_factor
    return float(max(Decimal(0), min(Decimal(1), prob)))


def _round_pct(value):
    return float(Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def _find_invention_sources(product_type_id, sde):
    bp_result = sde.product_to_bp.get(product_type_id)
    if not bp_result:
        raise SDEError(f"No blueprint found manufacturing type {product_type_id}")

    bp_type_id = bp_result[0]
    results = []
    for src_bp_id, act_id in sde.product_to_all_bps.get(bp_type_id, []):
        if act_id != _INVENTION:
            continue
        src_info = sde.types.get(src_bp_id)
        if not src_info:
            continue

        probs = sde.probabilities.get((src_bp_id, _INVENTION), [])
        base_prob = next((float(p['probability']) for p in probs
                          if p['productTypeID'] == bp_type_id), None)
        products = sde.products.get((src_bp_id, _INVENTION), [])
        base_runs = next((int(p['quantity']) for p in products
                          if p['productTypeID'] == bp_type_id), None)
        if base_prob is None or base_runs is None:
            continue

        results.append((src_bp_id, src_info['typeName'], base_prob, base_runs))

    if not results:
        raise SDEError(f"No invention source found for type {product_type_id}")
    return results


def _science_skill_count(skill_names):
    return sum(1 for s in skill_names if 'Encryption Methods' not in s)


def _build_prob_rows(sources, science_count, skill_level, is_t3):
    rows = []
    for source in (sources if is_t3 else [sources[0]]):
        _, label, base_prob, base_runs = source
        for dec in DECRYPTORS:
            prob = _invention_prob(base_prob, skill_level, science_count, dec['prob_mult'])
            row = {
                'decryptor': dec['name'],
                'probability': _round_pct(prob * 100),
                'runs': base_runs + dec['runs'],
                'me': 2 + dec['me'],
                'te': 4 + dec['te'],
            }
            if is_t3:
                row['relic_name'] = label
            rows.append(row)
    return rows


def get_invention(target_name, sde, skill_level=5):
    resolved = _resolve_target(target_name, sde)
    product_type_id = resolved['product_type_id']
    display_info = resolved['display_info']

    tech = sde.tech_level(product_type_id)
    if tech not in ('Tech II', 'Tech III'):
        raise SDEError(f"'{display_info['typeName']}' is {tech}, not Tech II or Tech III")

    is_t3 = tech == 'Tech III'
    ctx = sde.display_context(display_info)

    sources = _find_invention_sources(product_type_id, sde)

    if is_t3:
        def relic_sort_key(r):
            for i, q in enumerate(RELIC_QUALITY_ORDER):
                if r[1].startswith(q):
                    return i
            return len(RELIC_QUALITY_ORDER)
        sources.sort(key=relic_sort_key)

    first_bp = sources[0][0]
    skill_names = sde.named_skills(first_bp, _INVENTION)
    datacores = sde.named_materials(first_bp, _INVENTION)
    time_seconds = sde.time.get((first_bp, _INVENTION), 0)
    science_count = _science_skill_count(skill_names)

    result = {
        'type_id': product_type_id,
        'type_name': display_info['typeName'],
        'category_name': ctx['category_name'],
        'group_name': ctx['group_name'],
        'market_group_path': ctx['market_group_path'],
        'tech_level': tech,
        'datacores': datacores,
        'required_per_run': datacores[0]['quantity'] if datacores else 0,
        'skills': skill_names,
        'time': math.ceil(time_seconds / 60),
        'science_skill_count': science_count,
        'prob_rows': _build_prob_rows(sources, science_count, skill_level, is_t3),
    }

    if is_t3:
        result['relics'] = sources
    else:
        result['base_bp_name'] = sde.type_name(sources[0][0])
        result['base_bp_id'] = sources[0][0]
        result['base_prob'] = sources[0][2]
        result['base_runs'] = sources[0][3]

    return result


def format_invention(inv):
    lines = [
        f"ITEM: {inv['type_name']} ({inv['type_id']})",
        f"Group: {inv['category_name']} > {inv['group_name']}",
        f"Market Group: {inv['market_group_path'] or 'None'}",
        f"Tech Level: {inv['tech_level']}",
        "",
        "Invention:",
    ]

    if inv['tech_level'] == 'Tech II':
        lines.append(f"Base: {inv['base_bp_name']} ({inv['base_bp_id']})")

    lines.append(f"Datacores: {', '.join(d['name'] for d in inv['datacores'])}")
    lines.append(f"Required Per Run: {inv['required_per_run']}")
    lines.append(f"Skills: {', '.join(inv['skills'])}")
    lines.append(f"Time: {inv['time']}")

    lines.append("Probabilities:")
    if inv['tech_level'] == 'Tech II':
        lines.append("| Decryptor                       | Probability | Runs | ME | TE |")
        lines.append("|---------------------------------|-------------|------|----|----|")
        for row in inv['prob_rows']:
            lines.append(
                f"| {row['decryptor']:<32}| {row['probability']:<11.2f} "
                f"| {row['runs']:<4} | {row['me']:<2} | {row['te']:<2} |"
            )
    else:
        lines.append("| Invented From                | Decryptor                       | Probability | Runs | ME | TE |")
        lines.append("|------------------------------|---------------------------------|-------------|------|----|----|")
        for row in inv['prob_rows']:
            lines.append(
                f"| {row['relic_name']:<28} | {row['decryptor']:<32}"
                f"| {row['probability']:<11.2f} | {row['runs']:<4} "
                f"| {row['me']:<2} | {row['te']:<2} |"
            )

    return '\n'.join(lines)


# ── plan ─────────────────────────────────────────────────────────

def _is_int(s):
    try:
        int(s)
        return True
    except ValueError:
        return False


# Map first-word -> list of (full_name, word_count) for multi-word decryptors
_DECRYPTOR_BY_FIRST = {}
for _d in DECRYPTORS:
    _DECRYPTOR_BY_FIRST.setdefault(_d['name'].split()[0], []).append(
        (_d['name'], len(_d['name'].split()))
    )


def _find_decryptor_at(tokens, pos):
    if pos >= len(tokens):
        return None, 0
    candidates = _DECRYPTOR_BY_FIRST.get(tokens[pos], [])
    for full_name, wc in sorted(candidates, key=lambda x: -x[1]):
        if pos + wc <= len(tokens) and ' '.join(tokens[pos:pos + wc]) == full_name:
            return full_name, wc
    return None, 0


def _parse_build_string(build_str):
    """Parse a build string into structured plan parameters.

    Supported formats:
      Item:       {Item name} [{Runs Per Job}] [x{Number of Jobs}]
      Normal:     {Item name} {Runs Per Job} {ME} {TE} [x{Number of Jobs}]
      Invention:  {Item name} {Runs Per Job} {DecryptorName|None} [x{Number of Jobs}]
      Rev Eng:    {Item name} {RunsPerJob} {DecryptorName|None} {RelicName} [x{NumJobs}]
    """
    tokens = build_str.strip().split()

    num_jobs = 1
    if tokens and tokens[-1].startswith('x'):
        try:
            num_jobs = int(tokens[-1][1:])
        except ValueError:
            raise SDEError(f"Invalid job count in '{tokens[-1]}'")
        tokens = tokens[:-1]
        if not tokens:
            raise SDEError("Build string is empty after removing job count")

    # Scan left-to-right for the first integer (potential runs_per_job).
    for runs_pos, tok in enumerate(tokens):
        if not _is_int(tok):
            continue

        name = ' '.join(tokens[:runs_pos])
        if not name:
            continue

        runs = int(tokens[runs_pos])
        rest = tokens[runs_pos + 1:]

        # Normal: {name} {runs} {ME} {TE}
        if len(rest) == 2 and _is_int(rest[0]) and _is_int(rest[1]):
            return {'name': name, 'format': 'normal', 'runs_per_job': runs,
                    'me': int(rest[0]), 'te': int(rest[1]),
                    'decryptor': None, 'relic': None, 'num_jobs': num_jobs}

        # Invention / Reverse Engineering: {name} {runs} {Decryptor} [{RelicName...}]
        dec_name, dec_wc = _find_decryptor_at(rest, 0)
        if dec_name is not None:
            after_dec = rest[dec_wc:]
            if not after_dec:
                return {'name': name, 'format': 'invention',
                        'runs_per_job': runs, 'decryptor': dec_name,
                        'relic': None, 'num_jobs': num_jobs}
            return {'name': name, 'format': 'reverse_engineering',
                    'runs_per_job': runs, 'decryptor': dec_name,
                    'relic': ' '.join(after_dec), 'num_jobs': num_jobs}

        # Item with explicit runs: {name} {runs}
        if not rest:
            return {'name': name, 'format': 'item', 'runs_per_job': runs,
                    'me': 0, 'te': 0, 'decryptor': None, 'relic': None,
                    'num_jobs': num_jobs}

    # No integer found: item format with defaults.
    name = ' '.join(tokens)
    if not name:
        raise SDEError("Build string is empty")
    return {'name': name, 'format': 'item', 'runs_per_job': 1,
            'me': 0, 'te': 0, 'decryptor': None, 'relic': None,
            'num_jobs': num_jobs}


def _compute_days_per_run(time_seconds, te, te_bonus):
    """Compute days per run with TE and te_bonus applied."""
    return (time_seconds / _SECONDS_PER_DAY) * (1 - te / 100.0) * (1 - te_bonus / 100.0)


def _split_job(total_runs, days_per_run, split_days):
    """Split a job if it exceeds the split_days threshold.

    Returns a list of (runs, count) tuples representing jobs to create.
    Each job will have days <= split_days.
    """
    max_split = min(30.0, split_days)

    # Check if splitting is needed
    total_days = total_runs * days_per_run
    if total_days <= max_split:
        # No splitting needed
        return [(total_runs, 1)]

    # Can only split jobs with more than 1 run
    if total_runs <= 1:
        return [(total_runs, 1)]

    # Calculate max runs per job
    max_runs = math.floor(max_split / days_per_run)
    if max_runs < 1:
        max_runs = 1

    # Calculate split using the algorithm from spec
    t = math.ceil(total_runs / max_runs)
    B = total_runs // t
    x = total_runs % t

    # Check if we should adjust t
    if B + 1 == max_runs and x > t / 2:
        if total_runs // (t + 1) + 1 < max_runs:
            t = t + 1
            B = total_runs // t
            x = total_runs % t

    # Build output: x jobs of (B+1) runs, (t-x) jobs of B runs
    result = []
    if x > 0:
        result.append((B + 1, x))
    if t - x > 0:
        result.append((B, t - x))

    return result


def _compute_plan_materials(bp_id, activity_id, me, te, te_bonus,
                            runs_per_job, num_jobs, sde):
    """Compute materials and job time for a manufacturing plan."""
    raw_mats = sde.materials.get((bp_id, activity_id), [])
    time_seconds = sde.time.get((bp_id, activity_id), 0)

    total_runs = runs_per_job * num_jobs

    days_per_run = _compute_days_per_run(time_seconds, te, te_bonus)
    days_per_job = days_per_run * runs_per_job

    apply_me = activity_id == _MANUFACTURING and me > 0
    materials = defaultdict(float)
    for m in raw_mats:
        mat_info = sde.types.get(m['materialTypeID'])
        if not mat_info:
            continue
        base = int(m['quantity'])
        per_run = max(1.0, base * (1 - me / 100.0)) if apply_me else float(base)
        materials[mat_info['typeName']] += round(per_run * total_runs, 10)

    return materials, days_per_job, days_per_run


def _add_materials(total, more):
    for name, qty in more.items():
        total[name] += qty


def _is_t1_capital_component(product_type_id, bp_id, sde):
    """Check if a blueprint produces a T1 Capital ship component.

    T1 Capital components are items used in capital ship construction.
    They use ME=0 and TE=0 when built as components.
    """
    # Check if the product is in the Capital Ship Components group
    product_info = sde.types.get(str(product_type_id))
    if not product_info:
        return False

    grp = sde.groups.get(product_info.get('groupID'))
    if not grp:
        return False

    # Capital ship components are in specific groups
    # Common capital component group names contain "Capital" or specific component types
    grp_name = grp.get('groupName', '')
    capital_component_groups = [
        'Capital Armor Plates',
        'Capital Battery',
        'Capital Booster',
        'Capital Capacitor Battery',
        'Capital Cargo Bay',
        'Capital Computer System',
        'Capital Construction Parts',
        'Capital Corporate Hangar Bay',
        'Capital Drone Bay',
        'Capital Engineering Systems',
        'Capital Excavation Systems',
        'Capital Jump Drive',
        'Capital Propulsion Engine',
        'Capital Sensor Cluster',
        'Capital Shield Emitter',
        'Capital Ship Bay',
        'Capital Solar Array',
        'Capital Targeting System',
        'Capital Warfare Processor',
        'Advanced Capital Construction Components',
    ]

    # Check if this is a capital component
    if grp_name in capital_component_groups:
        # Verify it's Tech I (not Tech II)
        tech = sde.tech_level(str(product_type_id))
        return tech == 'Tech I'

    return False


def _run_build_all(all_materials, jobs, needed_materials, config, sde):
    """Recursively build all buildable materials.

    Args:
        all_materials: Dict to accumulate materials to buy (non-buildable)
        jobs: List to accumulate job entries
        needed_materials: Dict of material name -> quantity needed
        config: Config dict with split_days, component_bpo_defaults, te_bonus
        sde: SDE instance
    """
    split_days = config.get('split_days', 3.0)
    if split_days <= 0:
        split_days = 3.0
    te_bonus = config.get('te_bonus', 0)

    # Get component BPO defaults
    comp_defaults = config.get('component_bpo_defaults', {})
    comp_me = comp_defaults.get('me', 10)
    comp_te = comp_defaults.get('te', 20)

    # Validate ranges
    comp_me = max(0, min(10, comp_me))
    comp_te = max(0, min(20, comp_te))

    # Process materials in waves until no more buildable materials remain
    while True:
        # Find buildable materials that we need
        to_build = {}
        for name, qty in list(needed_materials.items()):
            if qty <= 0:
                continue
            type_info = sde.types_by_name.get(name)
            if type_info and type_info['typeID'] in sde.buildable:
                to_build[name] = qty

        if not to_build:
            # No more buildable materials - remaining go to buy list
            for name, qty in needed_materials.items():
                if qty > 0:
                    all_materials[name] += qty
            break

        # Build each material
        for name, qty_needed in to_build.items():
            type_info = sde.types_by_name.get(name)
            if not type_info:
                continue

            type_id = type_info['typeID']
            bp_id, activity_id = sde.product_to_bp.get(type_id, (None, None))
            if bp_id is None:
                continue

            # Get product quantity per run
            products = sde.products.get((bp_id, activity_id), [])
            if not products:
                continue
            output_qty = int(products[0]['quantity'])

            # Calculate runs needed (round up to get at least qty_needed)
            runs_needed = math.ceil(qty_needed / output_qty)

            # Determine ME/TE for this component
            if activity_id == _MANUFACTURING:
                # Check if this is a T1 Capital ship component
                if _is_t1_capital_component(type_id, bp_id, sde):
                    me, te = 0, 0
                else:
                    me, te = comp_me, comp_te
            else:
                # Reactions don't use ME/TE
                me, te = 0, 0

            # Get blueprint time
            time_seconds = sde.time.get((bp_id, activity_id), 0)
            days_per_run = _compute_days_per_run(time_seconds, te, te_bonus)

            # Split job if needed
            split_jobs = _split_job(runs_needed, days_per_run, split_days)

            # Add jobs
            for runs, count in split_jobs:
                days = round(runs * days_per_run, 2)
                jobs.append({
                    'name': sde.bp_name(bp_id),
                    'runs': runs,
                    'days': days,
                    'count': count,
                })

            # Compute materials for this build
            raw_mats = sde.materials.get((bp_id, activity_id), [])
            apply_me = activity_id == _MANUFACTURING and me > 0

            # Add materials to needed_materials (will be processed in next iteration)
            for m in raw_mats:
                mat_info = sde.types.get(m['materialTypeID'])
                if not mat_info:
                    continue
                base = int(m['quantity'])
                per_run = max(1.0, base * (1 - me / 100.0)) if apply_me else float(base)
                mat_name = mat_info['typeName']
                needed_materials[mat_name] = needed_materials.get(mat_name, 0) + round(per_run * runs_needed, 10)

            # Calculate surplus (overproduction)
            produced = runs_needed * output_qty
            surplus = produced - qty_needed

            # Update end_amount for this material (will be calculated later)
            # For now, just remove the needed amount from the list
            del needed_materials[name]

            # If there's surplus, it becomes an end_amount for this material
            if surplus > 0:
                # Track surplus separately - it's not bought but produced as byproduct
                all_materials[name] = all_materials.get(name, 0) - surplus


def _attempts_to_bps(total_runs, bp_max_runs, prob):
    """Invention/RE attempts needed to produce enough BPs for total_runs."""
    bps_needed = math.ceil(total_runs / bp_max_runs)
    if prob <= 0:
        return float('inf')
    return math.ceil(bps_needed / Decimal(str(prob)))


def _pick_decryptor(name):
    dec = next((d for d in DECRYPTORS if d['name'] == name), None)
    if dec is None:
        raise SDEError(f"Unknown decryptor: '{name}'")
    return dec


def _add_decryptor_materials(all_materials, dec, attempts, sde):
    if dec['name'] == 'None' or dec['name'] not in sde.types_by_name:
        return
    all_materials[dec['name']] += int(attempts)


def _add_invention_or_re_materials(all_materials, src_bp_id, attempts, sde):
    for m in sde.named_materials(src_bp_id, _INVENTION):
        all_materials[m['name']] += int(attempts) * m['quantity']


def run_plan(build_str, config_path, sde_dir, output_dir, sde, build_all=False):
    parsed = _parse_build_string(build_str)

    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    te_bonus = config.get('te_bonus', 0)
    invention_skill = config.get('invention_skill', 5)

    resolved = _resolve_target(parsed['name'], sde)
    product_type_id = resolved['product_type_id']
    bp_id = resolved['blueprint_type_id']
    activity_id = resolved['activity_id']

    fmt = parsed['format']

    all_materials = defaultdict(float)
    jobs = []

    # Determine ME/TE for manufacturing step (and any invention/RE inputs).
    if fmt == 'item':
        mfg_me, mfg_te = 0, 0
    elif fmt == 'normal':
        mfg_me, mfg_te = parsed['me'], parsed['te']
    elif fmt == 'invention':
        sources = _find_invention_sources(product_type_id, sde)
        _run_invention_or_re_plan(
            all_materials, parsed, sources[0], sde, invention_skill,
        )
        dec = _pick_decryptor(parsed['decryptor'])
        mfg_me = 2 + dec['me']
        mfg_te = 4 + dec['te']
    elif fmt == 'reverse_engineering':
        sources = _find_invention_sources(product_type_id, sde)
        source = next((s for s in sources if s[1] == parsed['relic']), None)
        if source is None:
            raise SDEError(
                f"Relic '{parsed['relic']}' not found. "
                f"Available: {[s[1] for s in sources]}"
            )
        _run_invention_or_re_plan(
            all_materials, parsed, source, sde, invention_skill, is_re=True,
            relic_name=parsed['relic'],
        )
        dec = _pick_decryptor(parsed['decryptor'])
        mfg_me = 2 + dec['me']
        mfg_te = 4 + dec['te']
    else:
        raise SDEError(f"Unknown format: {fmt}")

    # Manufacturing step (common to all formats).
    total_runs = parsed['runs_per_job'] * parsed['num_jobs']
    mats, days, days_per_run = _compute_plan_materials(
        bp_id, activity_id, mfg_me, mfg_te, te_bonus,
        parsed['runs_per_job'], parsed['num_jobs'], sde,
    )

    split_days = config.get('split_days', 3.0)
    if split_days <= 0:
        split_days = 3.0

    # Add the top-level manufacturing job(s) with splitting
    split_jobs = _split_job(total_runs, days_per_run, split_days)
    for runs, count in split_jobs:
        job_days = round(runs * days_per_run, 2)
        jobs.append({
            'name': sde.bp_name(bp_id),
            'runs': runs,
            'days': job_days,
            'count': count,
        })

    if build_all:
        # Recursively build all buildable materials
        _run_build_all(all_materials, jobs, dict(mats), config, sde)
    else:
        _add_materials(all_materials, mats)

    _write_plan_outputs(output_dir, all_materials, jobs, sde)


def _run_invention_or_re_plan(all_materials, parsed, source, sde,
                              invention_skill, is_re=False, relic_name=None):
    """Accumulate invention or reverse-engineering inputs into all_materials.

    The manufacturing step is handled by the caller; this only adds the
    invention/RE attempts' materials (datacores, decryptors, optional relic).
    """
    src_bp_id, _, base_prob, base_runs = source
    dec = _pick_decryptor(parsed['decryptor'])

    skill_names = sde.named_skills(src_bp_id, _INVENTION)
    science_count = _science_skill_count(skill_names)
    prob = _invention_prob(base_prob, invention_skill, science_count, dec['prob_mult'])

    total_mfg_runs = parsed['runs_per_job'] * parsed['num_jobs']
    bp_max_runs = base_runs + dec['runs']
    attempts = _attempts_to_bps(total_mfg_runs, bp_max_runs, prob)

    _add_invention_or_re_materials(all_materials, src_bp_id, attempts, sde)
    _add_decryptor_materials(all_materials, dec, attempts, sde)

    if is_re and relic_name in sde.types_by_name:
        all_materials[relic_name] += int(attempts)


def _write_plan_outputs(output_dir, all_materials, jobs, sde):
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    mat_rows = []
    for name in sorted(all_materials):
        qty = all_materials[name]
        if qty < 0:
            # Negative means surplus (end_amount)
            end_amount = int(-qty) if -qty == int(-qty) else -qty
            to_buy = 0
        else:
            end_amount = 0
            to_buy = int(qty) if qty == int(qty) else qty
        type_info = sde.types_by_name.get(name)
        vol = sde.volume(type_info) if type_info else 0.0
        mat_rows.append({
            'name': name,
            'to_buy': to_buy,
            'buy_volume': round(to_buy * vol, 10),
            'start_amount': 0,
            'end_amount': end_amount,
        })

    with open(out / 'materials.csv', 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(
            f, fieldnames=['name', 'to_buy', 'buy_volume', 'start_amount', 'end_amount']
        )
        writer.writeheader()
        writer.writerows(mat_rows)

    jobs.sort(key=lambda j: (j['name'], j['runs'], j['count'], j['days']))
    with open(out / 'jobs.csv', 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=['name', 'runs', 'days', 'count'])
        writer.writeheader()
        writer.writerows(jobs)


# ── CLI ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='EVE Online Industry Recipe Planner')
    sub = parser.add_subparsers(dest='command')

    rp = sub.add_parser('recipe', help='Get recipe for a product or blueprint')
    rp.add_argument('target', help='Product or Blueprint name (exact match)')
    rp.add_argument('--sde', required=True, help='Path to SDE directory')
    rp.add_argument('--material-efficiency', '-me', type=int, default=0,
                    choices=range(0, 11), metavar='{0-10}',
                    help='Material efficiency level 0-10 (default: 0)')
    rp.add_argument('--time-efficiency', '-te', type=int, default=0,
                    choices=range(0, 21, 2), metavar='{0-20 even}',
                    help='Time efficiency level 0-20 even (default: 0)')
    rp.add_argument('--display-waste', '-waste', action='store_true',
                    help='Display waste due to ME not being max')

    ip = sub.add_parser(
        'invention',
        help='Get invention report for a Tech II/III product or blueprint',
    )
    ip.add_argument('target', help='Product or Blueprint name (exact match)')
    ip.add_argument('--sde', required=True, help='Path to SDE directory')
    ip.add_argument('--skill', type=int, default=5, choices=[1, 2, 3, 4, 5],
                    help='Skill level for all relevant skills (default: 5)')

    pp = sub.add_parser('plan', help='Create a build plan for an item')
    pp.add_argument(
        'build',
        help='Build string (item, normal, invention, or reverse engineering format)',
    )
    pp.add_argument('--config', required=True, help='Path to YAML build config file')
    pp.add_argument('--sde', required=True, help='Path to SDE directory')
    pp.add_argument('--output', required=True, help='Path to output directory')
    pp.add_argument('--build-all', action='store_true',
                    help='Build all intermediate materials recursively')

    args = parser.parse_args()

    if args.command not in ('recipe', 'invention', 'plan'):
        parser.print_help()
        sys.exit(1)

    if not Path(args.sde).is_dir():
        print(f"Error: SDE directory '{args.sde}' not found", file=sys.stderr)
        sys.exit(1)

    try:
        sde = SDE(args.sde)
    except Exception as e:
        print(f"Error loading SDE: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        if args.command == 'recipe':
            print(format_recipe(get_recipe(
                args.target, sde,
                me=args.material_efficiency,
                te=args.time_efficiency,
                display_waste=args.display_waste,
            )))
        elif args.command == 'invention':
            print(format_invention(get_invention(args.target, sde, args.skill)))
        elif args.command == 'plan':
            run_plan(args.build, args.config, args.sde, args.output, sde,
                     build_all=args.build_all)
    except SDEError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
