#!/usr/bin/env python3
"""
EVE Online Industry Recipe Planner

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


class SDEError(Exception):
    pass


class SDE:
    def __init__(self, sde_dir):
        p = Path(sde_dir)

        def bz2_csv(name):
            with bz2.open(p / name, 'rt', encoding='utf-8') as f:
                return list(csv.DictReader(f))

        def index(rows, key):
            return {row[key]: row for row in rows}

        def group(rows, *keys):
            keys = tuple(keys)
            out = defaultdict(list)
            for row in rows:
                out[tuple(row[k] for k in keys)].append(row)
            return dict(out)

        inv_types = bz2_csv('invTypes.csv.bz2')
        inv_groups = bz2_csv('invGroups.csv.bz2')
        inv_categories = bz2_csv('invCategories.csv.bz2')

        self.types = index(inv_types, 'typeID')
        self.types_by_name = {t['typeName']: t for t in inv_types}
        self.groups = index(inv_groups, 'groupID')
        self.categories = index(inv_categories, 'categoryID')
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
        self.product_to_all_bps = dict(self.product_to_all_bps)

    # ── type lookups ──────────────────────────────────────────────────

    def type_name(self, type_id):
        info = self.types.get(type_id)
        return info['typeName'] if info else 'Unknown'

    def is_blueprint(self, type_info):
        grp = self.groups.get(type_info['groupID'])
        return grp is not None and grp['categoryID'] == '9'

    def market_group_path(self, mg_id):
        if not mg_id or mg_id not in self.market_groups:
            return None
        parts = []
        cur = mg_id
        while cur and cur in self.market_groups:
            mg = self.market_groups[cur]
            parts.append(mg['marketGroupName'])
            cur = mg['parentGroupID']
            if not cur or cur == 'None':
                break
        return ' > '.join(reversed(parts))

    def tech_level(self, type_id):
        mt = self.meta_types.get(type_id)
        if not mt:
            return 'Tech I'
        mg = self.meta_groups.get(mt['metaGroupID'])
        if not mg:
            return 'Tech I'
        name = mg['metaGroupName']
        return name if name in _TECH_LEVELS else 'Tech I'

    def volume(self, type_info):
        cat = self.categories.get(self.groups.get(type_info['groupID'], {}).get('categoryID'))
        if cat and cat['categoryID'] == '6':
            grp_name = self.groups.get(type_info['groupID'], {}).get('groupName', '')
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
        """Return the common display metadata for a type."""
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
            key=lambda x: x.lower(),
        )


_TECH_LEVELS = {'Tech I', 'Tech II', 'Tech III'}


# ── error helper ──────────────────────────────────────────────────

def _fail(msg):
    raise SDEError(msg)


# ── target resolution ─────────────────────────────────────────────

def _resolve_target(target_name, sde):
    type_info = sde.types_by_name.get(target_name)
    if not type_info:
        _fail(f"'{target_name}' not found in SDE")
    if type_info['published'] != '1':
        _fail(f"'{target_name}' is not published")

    type_id = type_info['typeID']

    if sde.is_blueprint(type_info):
        bp_id = type_id
        products = sde.products.get((bp_id, '1'))
        activity_id = '1'
        if not products:
            products = sde.products.get((bp_id, '11'))
            activity_id = '11'
        if not products:
            _fail(f"Blueprint '{target_name}' has no manufacturing or reaction products")

        product = products[0]
        product_type_id = product['productTypeID']
        display_info = sde.types.get(product_type_id)
        if not display_info:
            _fail(f"Product type ID {product_type_id} not found")
        output_quantity = int(product['quantity'])
    else:
        product_type_id = type_id
        result = sde.product_to_bp.get(product_type_id)
        if not result:
            _fail(f"No blueprint found for '{target_name}'")
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


# ── recipe ────────────────────────────────────────────────────────

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
    apply_me = activity_id == '1'

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

    vol = sde.volume(display_info)
    return {
        'type_id': display_info['typeID'],
        'type_name': display_info['typeName'],
        'category_name': ctx['category_name'],
        'group_name': ctx['group_name'],
        'market_group_path': ctx['market_group_path'],
        'tech_level': ctx['tech_level'],
        'volume': vol,
        'activity_name': activity_name,
        'output_quantity': resolved['output_quantity'],
        'run_time': time_minutes,
        'materials': materials,
        'display_waste': display_waste,
    }


def format_recipe(recipe):
    lines = [
        f"ITEM: {recipe['type_name']} ({recipe['type_id']})",
        f"Group: {recipe['category_name']} > {recipe['group_name']}",
        f"Market Group: {recipe['market_group_path'] or 'None'}",
        f"Tech Level: {recipe['tech_level']}",
        f"Volume: {int(recipe['volume'])}.00" if recipe['volume'] == int(recipe['volume'])
        else f"Volume: {recipe['volume']:.2f}",
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
            lines.append(f"| {mat['name']} | {mat['quantity']} | {waste_str}| {'Yes' if mat['buildable'] else 'No'} |")
    else:
        lines.append("| Item | Quantity | Buildable |")
        lines.append("|:-:|:---:|---:|")
        for mat in recipe['materials']:
            lines.append(f"| {mat['name']} | {mat['quantity']} | {'Yes' if mat['buildable'] else 'No'} |")

    return '\n'.join(lines)


# ── decryptors & invention ────────────────────────────────────────

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
        _fail(f"No blueprint found manufacturing type {product_type_id}")

    bp_type_id = bp_result[0]
    results = []
    for src_bp_id, act_id in sde.product_to_all_bps.get(bp_type_id, []):
        if act_id != '8':
            continue
        src_info = sde.types.get(src_bp_id)
        if not src_info:
            continue

        probs = sde.probabilities.get((src_bp_id, '8'), [])
        base_prob = next((float(p['probability']) for p in probs
                          if p['productTypeID'] == bp_type_id), None)
        if base_prob is None:
            continue

        products = sde.products.get((src_bp_id, '8'), [])
        base_runs = next((int(p['quantity']) for p in products
                          if p['productTypeID'] == bp_type_id), None)
        if base_runs is None:
            continue

        results.append((src_bp_id, src_info['typeName'], base_prob, base_runs))

    if not results:
        _fail(f"No invention source found for type {product_type_id}")
    return results


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
        _fail(f"'{display_info['typeName']}' is {tech}, not Tech II or Tech III")

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
    skill_names = sde.named_skills(first_bp, '8')
    datacores = sde.named_materials(first_bp, '8')
    time_seconds = sde.time.get((first_bp, '8'), 0)
    science_count = sum(1 for s in skill_names if 'Encryption Methods' not in s)

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
                f"| {row['decryptor']:<32}| {row['probability']:<11.2f} | {row['runs']:<4} | {row['me']:<2} | {row['te']:<2} |"
            )
    else:
        lines.append("| Invented From                | Decryptor                       | Probability | Runs | ME | TE |")
        lines.append("|------------------------------|---------------------------------|-------------|------|----|----|")
        for row in inv['prob_rows']:
            lines.append(
                f"| {row['relic_name']:<28} | {row['decryptor']:<32}| {row['probability']:<11.2f} | {row['runs']:<4} | {row['me']:<2} | {row['te']:<2} |"
            )

    return '\n'.join(lines)


# ── plan ───────────────────────────────────────────────────────────

def _parse_build_string(build_str):
    """Parse a build string into structured plan parameters.

    Supported formats:
      Item:       {Item name} [{Runs Per Job}] [x{Number of Jobs}]
      Normal:     {Item name} {Runs Per Job} {ME} {TE} [x{Number of Jobs}]
      Invention:  {Item name} {Runs Per Job} {DecryptorName|None} [x{Number of Jobs}]
      Rev Eng:    {Item name} {RunsPerJob} {DecryptorName|None} {RelicName} [x{NumJobs}]
    """
    tokens = build_str.strip().split()

    # ── extract optional x{NumJobs} from the end ──
    num_jobs = 1
    if tokens and tokens[-1].startswith('x'):
        try:
            num_jobs = int(tokens[-1][1:])
        except ValueError:
            _fail(f"Invalid job count in '{tokens[-1]}'")
        tokens = tokens[:-1]
        if not tokens:
            _fail("Build string is empty after removing job count")

    def is_int(s):
        try:
            int(s)
            return True
        except ValueError:
            return False

    # Build a lookup for multi-word decryptor names: token -> list of (name, word_count)
    decryptor_names = {d['name'] for d in DECRYPTORS}
    # Map first-word to list of (full_name, word_count)
    dec_by_first = {}
    for dn in decryptor_names:
        first = dn.split()[0]
        dec_by_first.setdefault(first, []).append((dn, len(dn.split())))

    def find_decryptor_at(tok_list, pos):
        """Check if tokens starting at pos form a known decryptor name.
        Returns (decryptor_name, tokens_consumed) or (None, 0).
        """
        if pos >= len(tok_list):
            return None, 0
        first = tok_list[pos]
        candidates = dec_by_first.get(first, [])
        # Try longest match first
        for full_name, wc in sorted(candidates, key=lambda x: -x[1]):
            if pos + wc <= len(tok_list):
                candidate = ' '.join(tok_list[pos:pos + wc])
                if candidate == full_name:
                    return full_name, wc
        return None, 0

    # Strategy: scan tokens from left to right looking for the first integer
    # that could be "runs_per_job". Everything before it is the item name.
    # Then parse the remainder according to format rules.

    # Find all positions where an integer appears (potential runs_per_job)
    for runs_pos in range(len(tokens)):
        if not is_int(tokens[runs_pos]):
            continue

        name = ' '.join(tokens[:runs_pos])
        if not name:
            continue

        runs = int(tokens[runs_pos])
        rest = tokens[runs_pos + 1:]

        # Try Normal format: {name} {runs} {ME} {TE}
        if len(rest) == 2 and is_int(rest[0]) and is_int(rest[1]):
            return {'name': name, 'format': 'normal', 'runs_per_job': runs,
                    'me': int(rest[0]), 'te': int(rest[1]),
                    'decryptor': None, 'relic': None, 'num_jobs': num_jobs}

        # Try Invention format: {name} {runs} {Decryptor}
        if len(rest) >= 1:
            dec_name, dec_wc = find_decryptor_at(rest, 0)
            if dec_name is not None:
                after_dec = rest[dec_wc:]
                if len(after_dec) == 0:
                    return {'name': name, 'format': 'invention',
                            'runs_per_job': runs, 'decryptor': dec_name,
                            'relic': None, 'num_jobs': num_jobs}
                # Try Reverse Engineering: {name} {runs} {Decryptor} {RelicName...}
                relic_name = ' '.join(after_dec)
                return {'name': name, 'format': 'reverse_engineering',
                        'runs_per_job': runs, 'decryptor': dec_name,
                        'relic': relic_name, 'num_jobs': num_jobs}

        # Item format with specified runs: {name} {runs}
        if len(rest) == 0:
            return {'name': name, 'format': 'item', 'runs_per_job': runs,
                    'me': 0, 'te': 0, 'decryptor': None, 'relic': None,
                    'num_jobs': num_jobs}

    # If no integer found, it's item format with defaults
    name = ' '.join(tokens)
    if not name:
        _fail("Build string is empty")
    return {'name': name, 'format': 'item', 'runs_per_job': 1,
            'me': 0, 'te': 0, 'decryptor': None, 'relic': None,
            'num_jobs': num_jobs}


def _load_blueprint_limits(sde_dir):
    p = Path(sde_dir)
    limits = {}
    with bz2.open(p / 'industryBlueprints.csv.bz2', 'rt', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            limits[row['typeID']] = int(row['maxProductionLimit'])
    return limits


def _compute_plan_materials(bp_id, activity_id, me, te, te_bonus, runs_per_job, num_jobs, sde):
    """Compute materials and job time for a manufacturing plan."""
    raw_mats = sde.materials.get((bp_id, activity_id), [])
    time_seconds = sde.time.get((bp_id, activity_id), 0)

    total_runs = runs_per_job * num_jobs

    # Time: Full TE factor = BP Time * (1 - BP TE/100) * (1 - TE bonus/100)
    bp_time_days = time_seconds / 86400.0
    days_per_run = bp_time_days * (1 - te / 100.0) * (1 - te_bonus / 100.0)
    days_per_job = days_per_run * runs_per_job

    # Materials per run: max(1, base * (1 - ME/100)), no ceiling
    apply_me = activity_id == '1' and me > 0
    materials = {}
    for m in raw_mats:
        mat_info = sde.types.get(m['materialTypeID'])
        if not mat_info:
            continue
        base = int(m['quantity'])
        if apply_me:
            per_run = max(1.0, base * (1 - me / 100.0))
        else:
            per_run = float(base)
        total = round(per_run * total_runs, 10)
        mat_name = mat_info['typeName']
        materials[mat_name] = materials.get(mat_name, 0.0) + total

    return materials, days_per_job


def run_plan(build_str, config_path, sde_dir, output_dir, sde):
    parsed = _parse_build_string(build_str)

    # Load config
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    te_bonus = config.get('te_bonus', 0)
    invention_skill = config.get('invention_skill', 5)

    # Resolve the product
    resolved = _resolve_target(parsed['name'], sde)
    product_type_id = resolved['product_type_id']
    bp_id = resolved['blueprint_type_id']
    activity_id = resolved['activity_id']

    fmt = parsed['format']

    all_materials = {}  # name -> total quantity (float)
    jobs = []           # list of {name, runs, days, count}

    if fmt == 'item':
        # ME=0, TE=0
        me, te = 0, 0
        mats, days = _compute_plan_materials(
            bp_id, activity_id, me, te, te_bonus,
            parsed['runs_per_job'], parsed['num_jobs'], sde)
        for name, qty in mats.items():
            all_materials[name] = all_materials.get(name, 0.0) + qty
        bp_type_info = sde.types.get(bp_id)
        bp_name = bp_type_info['typeName'] if bp_type_info else sde.type_name(bp_id)
        jobs.append({
            'name': bp_name,
            'runs': parsed['runs_per_job'],
            'days': round(days, 2),
            'count': parsed['num_jobs'],
        })

    elif fmt == 'normal':
        me, te = parsed['me'], parsed['te']
        mats, days = _compute_plan_materials(
            bp_id, activity_id, me, te, te_bonus,
            parsed['runs_per_job'], parsed['num_jobs'], sde)
        for name, qty in mats.items():
            all_materials[name] = all_materials.get(name, 0.0) + qty
        bp_type_info = sde.types.get(bp_id)
        bp_name = bp_type_info['typeName'] if bp_type_info else sde.type_name(bp_id)
        jobs.append({
            'name': bp_name,
            'runs': parsed['runs_per_job'],
            'days': round(days, 2),
            'count': parsed['num_jobs'],
        })

    elif fmt == 'invention':
        # Find invention source
        sources = _find_invention_sources(product_type_id, sde)
        # T2 uses first source
        source = sources[0]
        src_bp_id, src_name, base_prob, base_runs = source

        # Get decryptor
        dec = next((d for d in DECRYPTORS if d['name'] == parsed['decryptor']), None)
        if dec is None:
            _fail(f"Unknown decryptor: '{parsed['decryptor']}'")

        # Invented BP stats
        inv_me = 2 + dec['me']
        inv_te = 4 + dec['te']
        bp_max_runs = base_runs + dec['runs']

        # Calculate invention probability
        skill_names = sde.named_skills(src_bp_id, '8')
        science_count = sum(1 for s in skill_names if 'Encryption Methods' not in s)
        prob = _invention_prob(base_prob, invention_skill, science_count, dec['prob_mult'])

        # Total manufacturing runs needed
        total_mfg_runs = parsed['runs_per_job'] * parsed['num_jobs']

        # Number of BPs needed
        bps_needed = math.ceil(total_mfg_runs / bp_max_runs)

        # Invention attempts needed
        inv_attempts = math.ceil(bps_needed / Decimal(str(prob))) if prob > 0 else float('inf')

        # Invention materials (per attempt = per run)
        inv_raw_mats = sde.named_materials(src_bp_id, '8')
        for m in inv_raw_mats:
            total = int(inv_attempts) * m['quantity']
            all_materials[m['name']] = all_materials.get(m['name'], 0.0) + total

        # If decryptor is not None, add the decryptor as a material too
        if dec['name'] != 'None':
            dec_type = sde.types_by_name.get(dec['name'])
            if dec_type:
                all_materials[dec['name']] = all_materials.get(dec['name'], 0.0) + int(inv_attempts)

        # Manufacturing materials
        mats, days = _compute_plan_materials(
            bp_id, activity_id, inv_me, inv_te, te_bonus,
            parsed['runs_per_job'], parsed['num_jobs'], sde)
        for name, qty in mats.items():
            all_materials[name] = all_materials.get(name, 0.0) + qty

        # Job: manufacturing the product
        bp_type_info = sde.types.get(bp_id)
        bp_name = bp_type_info['typeName'] if bp_type_info else sde.type_name(bp_id)
        jobs.append({
            'name': bp_name,
            'runs': parsed['runs_per_job'],
            'days': round(days, 2),
            'count': parsed['num_jobs'],
        })

    elif fmt == 'reverse_engineering':
        # Find RE source that matches the relic name
        sources = _find_invention_sources(product_type_id, sde)

        # Find the source matching the relic name
        relic_name = parsed['relic']
        source = None
        for src in sources:
            if src[1] == relic_name:
                source = src
                break
        if source is None:
            available = [s[1] for s in sources]
            _fail(f"Relic '{relic_name}' not found. Available: {available}")

        src_bp_id, src_name, base_prob, base_runs = source

        # Get decryptor
        dec = next((d for d in DECRYPTORS if d['name'] == parsed['decryptor']), None)
        if dec is None:
            _fail(f"Unknown decryptor: '{parsed['decryptor']}'")

        # Invented BP stats (same formula as T2 invention)
        inv_me = 2 + dec['me']
        inv_te = 4 + dec['te']
        bp_max_runs = base_runs + dec['runs']

        # Calculate RE probability
        skill_names = sde.named_skills(src_bp_id, '8')
        science_count = sum(1 for s in skill_names if 'Encryption Methods' not in s)
        prob = _invention_prob(base_prob, invention_skill, science_count, dec['prob_mult'])

        # Total manufacturing runs needed
        total_mfg_runs = parsed['runs_per_job'] * parsed['num_jobs']

        # Number of BPs needed
        bps_needed = math.ceil(total_mfg_runs / bp_max_runs)

        # RE attempts needed
        re_attempts = math.ceil(bps_needed / Decimal(str(prob))) if prob > 0 else float('inf')

        # RE materials (per attempt)
        inv_raw_mats = sde.named_materials(src_bp_id, '8')
        for m in inv_raw_mats:
            total = int(re_attempts) * m['quantity']
            all_materials[m['name']] = all_materials.get(m['name'], 0.0) + total

        # Add the relic as a material
        relic_type = sde.types_by_name.get(relic_name)
        if relic_type:
            all_materials[relic_name] = all_materials.get(relic_name, 0.0) + int(re_attempts)

        # If decryptor is not None, add it too
        if dec['name'] != 'None':
            dec_type = sde.types_by_name.get(dec['name'])
            if dec_type:
                all_materials[dec['name']] = all_materials.get(dec['name'], 0.0) + int(re_attempts)

        # Manufacturing materials
        mats, days = _compute_plan_materials(
            bp_id, activity_id, inv_me, inv_te, te_bonus,
            parsed['runs_per_job'], parsed['num_jobs'], sde)
        for name, qty in mats.items():
            all_materials[name] = all_materials.get(name, 0.0) + qty

        # Job: manufacturing the product
        bp_type_info = sde.types.get(bp_id)
        bp_name = bp_type_info['typeName'] if bp_type_info else sde.type_name(bp_id)
        jobs.append({
            'name': bp_name,
            'runs': parsed['runs_per_job'],
            'days': round(days, 2),
            'count': parsed['num_jobs'],
        })

    # ── Write output CSVs ──
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # materials.csv
    mat_rows = []
    for name in sorted(all_materials.keys()):
        qty = all_materials[name]
        to_buy = int(qty) if qty == int(qty) else qty
        type_info = sde.types_by_name.get(name)
        vol = sde.volume(type_info) if type_info else 0.0
        buy_volume = round(to_buy * vol, 10)
        # Format buy_volume: use clean float representation
        mat_rows.append({
            'name': name,
            'to_buy': to_buy,
            'buy_volume': buy_volume,
            'start_amount': 0,
            'end_amount': 0,
        })

    with open(out / 'materials.csv', 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=['name', 'to_buy', 'buy_volume', 'start_amount', 'end_amount'])
        writer.writeheader()
        for row in mat_rows:
            writer.writerow(row)

    # jobs.csv - sort by name > runs > count > days
    jobs.sort(key=lambda j: (j['name'], j['runs'], j['count'], j['days']))

    with open(out / 'jobs.csv', 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=['name', 'runs', 'days', 'count'])
        writer.writeheader()
        for row in jobs:
            writer.writerow(row)


# ── CLI ───────────────────────────────────────────────────────────

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

    ip = sub.add_parser('invention', help='Get invention report for a Tech II/III product or blueprint')
    ip.add_argument('target', help='Product or Blueprint name (exact match)')
    ip.add_argument('--sde', required=True, help='Path to SDE directory')
    ip.add_argument('--skill', type=int, default=5, choices=[1, 2, 3, 4, 5],
                    help='Skill level for all relevant skills (default: 5)')

    pp = sub.add_parser('plan', help='Create a build plan for an item')
    pp.add_argument('build', help='Build string (item, normal, invention, or reverse engineering format)')
    pp.add_argument('--config', required=True, help='Path to YAML build config file')
    pp.add_argument('--sde', required=True, help='Path to SDE directory')
    pp.add_argument('--output', required=True, help='Path to output directory')

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
            run_plan(args.build, args.config, args.sde, args.output, sde)
    except SDEError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
