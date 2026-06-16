#!/usr/bin/env python3
"""Comprehensive slop analysis: before→after skill impact across all dimensions."""
import json
import os
import glob
import math
import difflib
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import numpy as np
from collections import defaultdict
from pathlib import Path

ROOT = "/shared_workspace_mfs/ximing/slop-code-bench"
OUT = f"{ROOT}/docs/report"
os.chdir(f"{ROOT}/outputs")

# ══════════════════════════════════════════════════════════════
# DATA COLLECTION
# ══════════════════════════════════════════════════════════════

def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except:
        return None

def count_tests(eval_file):
    d = read_json(eval_file)
    if not d: return None
    groups = {}
    for gn, gd in d.get("tests", {}).items():
        p = len(gd.get("passed", []))
        f_ = len(gd.get("failed", []))
        groups[gn] = (p, f_)
    total_p = sum(v[0] for v in groups.values())
    total_f = sum(v[1] for v in groups.values())
    return total_p, total_f, groups

def extract_quality(qpath):
    d = read_json(qpath)
    if not d: return None
    lint_counts = d.get("lint", {}).get("counts", {})
    return {
        "total_lines": d.get("lines", {}).get("total_lines", 0),
        "loc": d.get("lines", {}).get("loc", 0),
        "comments": d.get("lines", {}).get("comments", 0),
        "single_comment": d.get("lines", {}).get("single_comment", 0),
        "multi_comment": d.get("lines", {}).get("multi_comment", 0),
        "cc_max": d.get("complexity", {}).get("cc_max", 0),
        "cc_sum": d.get("complexity", {}).get("cc_sum", 0),
        "mi_min": d.get("complexity", {}).get("mi_min", 0),
        "cc_concentration": d.get("functions", {}).get("cc_concentration", 0),
        "cc_high_count": d.get("functions", {}).get("cc_high_count", 0),
        "cc_extreme_count": d.get("functions", {}).get("cc_extreme_count", 0),
        "cc_mean": d.get("functions", {}).get("cc_mean", 0),
        "func_count": d.get("functions", {}).get("count", 0),
        "func_lines_sum": d.get("functions", {}).get("lines_sum", 0),
        "func_lines_mean": d.get("functions", {}).get("lines_mean", 0),
        "depth_max": d.get("functions", {}).get("depth_max", 0),
        "clone_lines": d.get("redundancy", {}).get("clone_lines", 0),
        "clone_ratio": d.get("redundancy", {}).get("clone_ratio_sum", 0),
        "lint_errors": d.get("lint", {}).get("errors", 0),
        "lint_fixable": d.get("lint", {}).get("fixable", 0),
        "unused_vars": d.get("waste", {}).get("unused_variables", 0),
        "single_use_funcs": d.get("waste", {}).get("single_use_functions", 0),
        "trivial_wrappers": d.get("waste", {}).get("trivial_wrappers", 0),
        "verbosity_lines": d.get("verbosity_flagged_sloc_lines", 0),
        # Specific lint rules
        "C901": lint_counts.get("C901", 0),  # too complex
        "PLR0912": lint_counts.get("PLR0912", 0),  # too many branches
        "PLR1702": lint_counts.get("PLR1702", 0),  # too many nested blocks
        "F401": lint_counts.get("F401", 0),  # unused import
        "F841": lint_counts.get("F841", 0),  # unused variable
        # Structural
        "classes": d.get("classes", {}).get("count", 0),
        "methods": d.get("symbols", {}).get("methods", 0),
        "imports": d.get("symbols", {}).get("imports", 0),
        "file_count": d.get("file_count", 0),
        # Graph
        "propagation_cost": d.get("graph", {}).get("propagation_cost", 0),
        "dep_entropy": d.get("graph", {}).get("dependency_entropy", 0),
    }

def compute_erosion(q):
    """Compute paper's erosion metric from quality data."""
    if not q or q["func_lines_sum"] == 0:
        return None
    # Simplified: cc_concentration is already close
    return q["cc_concentration"]

def compute_verbosity(q):
    """Compute verbosity indicator."""
    if not q or q["loc"] == 0:
        return None
    return (q["clone_lines"] + q["verbosity_lines"]) / q["loc"]

def snapshot_line_counts(snap_dir):
    pys = sorted(glob.glob(f"{snap_dir}/*.py"))
    if not pys:
        return 0, 0
    total = 0
    for f in pys:
        try:
            total += sum(1 for _ in open(f))
        except:
            pass
    return len(pys), total

def diff_snapshots(before_dir, after_dir):
    """Diff source files between before and after snapshots."""
    before_pys = {os.path.basename(f): f for f in sorted(glob.glob(f"{before_dir}/*.py"))}
    after_pys = {os.path.basename(f): f for f in sorted(glob.glob(f"{after_dir}/*.py"))}

    all_files = sorted(set(list(before_pys.keys()) + list(after_pys.keys())))
    total_added = 0
    total_removed = 0
    changes = []

    for fname in all_files:
        if fname in before_pys and fname in after_pys:
            try:
                with open(before_pys[fname]) as f:
                    b_lines = f.readlines()
                with open(after_pys[fname]) as f:
                    a_lines = f.readlines()
                diff = list(difflib.unified_diff(b_lines, a_lines, lineterm=''))
                added = sum(1 for l in diff if l.startswith('+') and not l.startswith('+++'))
                removed = sum(1 for l in diff if l.startswith('-') and not l.startswith('---'))
                total_added += added
                total_removed += removed
                if added > 0 or removed > 0:
                    changes.append({"file": fname, "added": added, "removed": removed})
            except:
                pass
        elif fname in before_pys:
            try:
                n = sum(1 for _ in open(before_pys[fname]))
                total_removed += n
                changes.append({"file": fname, "added": 0, "removed": n, "deleted": True})
            except:
                pass
        elif fname in after_pys:
            try:
                n = sum(1 for _ in open(after_pys[fname]))
                total_added += n
                changes.append({"file": fname, "added": n, "removed": 0, "created": True})
            except:
                pass

    return {"added": total_added, "removed": total_removed, "net": total_added - total_removed, "files": changes}

# ── Collect from Pangu ──

pangu_runs = {
    "20260602T0203": ["cfgpipe","code_search","dag_execution","dynamic_buffer","etl_pipeline","eve_industry","eve_jump_planner"],
    "20260603T2011": ["database_migration","datagate"],
    "20260604T1409": ["circuit_eval","env_manager","eve_route_planner"],
    "20260605T1442": ["file_query_tool","log_query","migrate_configs","mvvault","pwd_manager"],
    "20260608T1615": ["file_backup","rejector"],
    "20260608T1902": ["dynamic_config_service_api","file_merger"],
    "20260610T0303": ["eve_market_tools","forge","l2m","meshctl","sith","textdrop","xjq"],
    "20260610T1405": ["mocked_http","recli","trajectory_api"],
    "20260610T1527": ["sheeteval","test_translator"],
    "20260611T0157": ["sith"],
}

# Also need baseline data for trajectory comparison
pangu_baselines = {
    "cfgpipe": "pangu/claude_code-2.0.51_baseline_20260603T1458",
    "circuit_eval": "pangu/claude_code-2.0.51_baseline_20260603T2011",
    "code_search": "pangu/claude_code-2.0.51_baseline_20260603T0121",
    "dag_execution": "pangu/claude_code-2.0.51_baseline_20260608T1608",
    "database_migration": "pangu/claude_code-2.0.51_baseline_20260604T1405",
    "datagate": "pangu/claude_code-2.0.51_baseline_20260603T2011",
    "dynamic_buffer": "pangu/claude_code-2.0.51_baseline_20260608T1608",
    "dynamic_config_service_api": "pangu/claude_code-2.0.51_baseline_20260608T1608",
    "env_manager": "pangu/claude_code-2.0.51_baseline_20260603T2011",
    "etl_pipeline": "pangu/claude_code-2.0.51_baseline_20260603T1458",
    "eve_industry": "pangu/claude_code-2.0.51_baseline_20260603T1458",
    "eve_jump_planner": "pangu/claude_code-2.0.51_baseline_20260603T1458",
    "eve_market_tools": "pangu/claude_code-2.0.51_baseline_20260604T1405",
    "eve_route_planner": "pangu/claude_code-2.0.51_baseline_20260604T1405",
    "file_backup": "pangu/claude_code-2.0.51_baseline_20260608T1608",
    "file_merger": "pangu/claude_code-2.0.51_baseline_20260608T1936",
    "file_query_tool": "pangu/claude_code-2.0.51_baseline_20260604T1405",
    "forge": "pangu/claude_code-2.0.51_baseline_20260604T1405",
    "l2m": "pangu/claude_code-2.0.51_baseline_20260605T1442",
    "log_query": "pangu/claude_code-2.0.51_baseline_20260604T1405",
    "meshctl": "pangu/claude_code-2.0.51_baseline_20260605T1442",
    "migrate_configs": "pangu/claude_code-2.0.51_baseline_20260605T1442",
    "mocked_http": "pangu/claude_code-2.0.51_baseline_20260608T2326",
    "mvvault": "pangu/claude_code-2.0.51_baseline_20260605T1442",
    "pwd_manager": "pangu/claude_code-2.0.51_baseline_20260605T1442",
    "recli": "pangu/claude_code-2.0.51_baseline_20260608T1608",
    "rejector": "pangu/claude_code-2.0.51_baseline_20260605T1442",
    "sheeteval": "pangu/claude_code-2.0.51_baseline_20260608T1608",
    "sith": "pangu/claude_code-2.0.51_baseline_20260605T1442",
    "test_translator": "pangu/claude_code-2.0.51_baseline_20260608T2326",
    "textdrop": "pangu/claude_code-2.0.51_baseline_20260608T1608",
    "trajectory_api": "pangu/claude_code-2.0.51_baseline_20260605T1442",
    "xjq": "pangu/claude_code-2.0.51_baseline_20260605T1442",
}

all_data = []  # skill before→after per checkpoint
baseline_trajectories = defaultdict(list)  # prob -> [(cp_num, quality)]
skill_trajectories = defaultdict(list)  # prob -> [(cp_num, before_quality, after_quality)]

seen_prob_cp = set()

for run_id, probs in pangu_runs.items():
    rr_dir = f"pangu/claude_code-2.0.51_review_refactor_{run_id}"
    if not os.path.isdir(rr_dir):
        continue
    for prob in probs:
        prob_dir = f"{rr_dir}/{prob}"
        if not os.path.isdir(prob_dir):
            continue
        for cp_dir in sorted(glob.glob(f"{prob_dir}/checkpoint_*/")):
            cpname = os.path.basename(cp_dir.rstrip('/'))
            cp_num = int(cpname.split('_')[1])

            key = f"{prob}/{cpname}"
            if key in seen_prob_cp:
                continue
            seen_prob_cp.add(key)

            before_eval = f"{cp_dir}evaluation.json"
            after_eval = f"{cp_dir}after_skill/evaluation.json"
            before_qual = f"{cp_dir}quality_analysis/overall_quality.json"
            after_qual = f"{cp_dir}after_skill/quality_analysis/overall_quality.json"
            traj_before = f"{cp_dir}trajectory_stats_before.json"
            traj_after = f"{cp_dir}after_skill/trajectory_stats_after.json"
            before_snap = f"{cp_dir}snapshot"
            after_snap = f"{cp_dir}after_skill/snapshot"

            if not os.path.exists(before_eval):
                continue

            bt = count_tests(before_eval)
            at = count_tests(after_eval) if os.path.exists(after_eval) else None
            bq = extract_quality(before_qual)
            aq = extract_quality(after_qual) if os.path.exists(after_qual) else None
            traj_b = read_json(traj_before)
            traj_a = read_json(traj_after)

            diff_data = None
            if os.path.isdir(before_snap) and os.path.isdir(after_snap):
                diff_data = diff_snapshots(before_snap, after_snap)

            if bt is None:
                continue

            entry = {
                "model": "pangu", "run": run_id, "problem": prob,
                "checkpoint": cpname, "cp_num": cp_num,
                "before_passed": bt[0], "before_failed": bt[1],
                "after_passed": at[0] if at else bt[0],
                "after_failed": at[1] if at else bt[1],
                "test_delta": (at[0] - bt[0]) if at else 0,
                "before_quality": bq, "after_quality": aq,
                "traj_before": traj_b, "traj_after": traj_a,
                "diff": diff_data,
                "erosion_before": compute_erosion(bq),
                "erosion_after": compute_erosion(aq) if aq else None,
                "verbosity_before": compute_verbosity(bq),
                "verbosity_after": compute_verbosity(aq) if aq else None,
            }
            all_data.append(entry)

            # Skill trajectory
            if bq:
                skill_trajectories[prob].append((cp_num, bq, aq))

# Baseline trajectories
for prob, bl_dir in pangu_baselines.items():
    for cp_dir in sorted(glob.glob(f"{bl_dir}/{prob}/checkpoint_*/")):
        cp_num = int(os.path.basename(cp_dir.rstrip('/')).split('_')[1])
        bq = extract_quality(f"{cp_dir}quality_analysis/overall_quality.json")
        if bq:
            baseline_trajectories[prob].append((cp_num, bq))

print(f"Collected {len(all_data)} checkpoint entries from {len(seen_prob_cp)} unique prob/cp pairs")
print(f"Baseline trajectories: {len(baseline_trajectories)} problems")

# ══════════════════════════════════════════════════════════════
# ANALYSIS
# ══════════════════════════════════════════════════════════════

# ── 1. Macro test impact ──
same = sum(1 for e in all_data if e["test_delta"] == 0)
improved = [e for e in all_data if e["test_delta"] > 0]
worsened = [e for e in all_data if e["test_delta"] < 0]

# ── 2. Quality metric deltas ──
quality_deltas = defaultdict(list)
for e in all_data:
    if e["before_quality"] and e["after_quality"]:
        for key in e["before_quality"]:
            delta = e["after_quality"][key] - e["before_quality"][key]
            quality_deltas[key].append(delta)

# ── 3. Smell categories ──
smell_categories = {
    "Erosion (TMB/HCC/RFC)": ["cc_max", "cc_sum", "cc_concentration", "cc_high_count", "C901", "PLR0912"],
    "Verbosity (PAU/Duplication)": ["clone_lines", "clone_ratio", "comments", "verbosity_lines", "total_lines", "loc"],
    "Waste (Dead Code)": ["unused_vars", "single_use_funcs", "trivial_wrappers", "F401"],
    "Nesting": ["depth_max", "PLR1702"],
    "Structural": ["func_count", "classes", "methods", "file_count"],
}

# ── 4. Skill agent behavior ──
skill_edits = defaultdict(int)
skill_turns = []
skill_tool_calls = []
skill_costs = []
no_change_count = 0
small_change_count = 0
large_change_count = 0

for e in all_data:
    if e["diff"]:
        net = abs(e["diff"]["net"])
        if net == 0 and e["diff"]["added"] == 0 and e["diff"]["removed"] == 0:
            no_change_count += 1
        elif e["diff"]["added"] + e["diff"]["removed"] <= 20:
            small_change_count += 1
        else:
            large_change_count += 1

    if e["traj_after"]:
        for tool, cnt in e["traj_after"].get("tools_used", {}).items():
            skill_edits[tool] += cnt
        skill_turns.append(e["traj_after"].get("turns", 0))
        skill_tool_calls.append(e["traj_after"].get("tool_calls", 0))
        skill_costs.append(e["traj_after"].get("cost", 0))

# ── 5. Erosion/Verbosity trajectories (baseline vs skill) ──
# Per checkpoint progress bin
def to_progress_bin(cp_num, max_cp):
    progress = cp_num / max_cp
    pb = math.ceil(progress * 5) / 5
    return max(pb, 0.2)

bin_labels = {0.2: 'Start', 0.4: 'Early', 0.6: 'Mid', 0.8: 'Late', 1.0: 'Final'}
bins_list = [0.2, 0.4, 0.6, 0.8, 1.0]

bl_erosion_bins = {b: [] for b in bins_list}
bl_verbosity_bins = {b: [] for b in bins_list}
sk_erosion_before_bins = {b: [] for b in bins_list}
sk_erosion_after_bins = {b: [] for b in bins_list}
sk_verbosity_before_bins = {b: [] for b in bins_list}
sk_verbosity_after_bins = {b: [] for b in bins_list}

for prob, traj in baseline_trajectories.items():
    max_cp = max(t[0] for t in traj)
    for cp_num, q in traj:
        pb = to_progress_bin(cp_num, max_cp)
        e = compute_erosion(q)
        v = compute_verbosity(q)
        if e is not None: bl_erosion_bins[pb].append(e)
        if v is not None: bl_verbosity_bins[pb].append(v)

for prob, traj in skill_trajectories.items():
    max_cp = max(t[0] for t in traj)
    for cp_num, bq, aq in traj:
        pb = to_progress_bin(cp_num, max_cp)
        eb = compute_erosion(bq)
        vb = compute_verbosity(bq)
        if eb is not None: sk_erosion_before_bins[pb].append(eb)
        if vb is not None: sk_verbosity_before_bins[pb].append(vb)
        if aq:
            ea = compute_erosion(aq)
            va = compute_verbosity(aq)
            if ea is not None: sk_erosion_after_bins[pb].append(ea)
            if va is not None: sk_verbosity_after_bins[pb].append(va)

# ── 6. Correlation: change size → test impact ──
change_vs_test = []
for e in all_data:
    if e["diff"]:
        change_size = e["diff"]["added"] + e["diff"]["removed"]
        change_vs_test.append((change_size, e["test_delta"], e["diff"]["net"]))

# ── 7. Cost analysis ──
total_skill_cost = sum(skill_costs)
total_test_gain = sum(e["test_delta"] for e in all_data if e["test_delta"] > 0)
total_test_loss = sum(e["test_delta"] for e in all_data if e["test_delta"] < 0)

# ── 8. Failure mode analysis ──
failure_modes = defaultdict(list)
for e in worsened:
    if e["diff"]:
        net = e["diff"]["net"]
        if net < -20:
            failure_modes["large_removal"].append(e)
        elif net > 20:
            failure_modes["large_addition"].append(e)
        elif abs(net) <= 5:
            failure_modes["small_change"].append(e)
        else:
            failure_modes["medium_change"].append(e)
    else:
        failure_modes["unknown"].append(e)

# ── 9. Slop rebound analysis ──
# For problems with multiple checkpoints, check if slop removed by skill comes back
rebound_cases = []
for prob, traj in skill_trajectories.items():
    traj_sorted = sorted(traj, key=lambda x: x[0])
    for i in range(len(traj_sorted) - 1):
        cp1_num, bq1, aq1 = traj_sorted[i]
        cp2_num, bq2, aq2 = traj_sorted[i + 1]
        if aq1 and bq2:
            # After skill at cp_i vs before skill at cp_{i+1}
            loc_after = aq1["total_lines"]
            loc_next_before = bq2["total_lines"]
            if loc_after > 0 and loc_next_before > loc_after * 1.1:
                rebound_cases.append({
                    "problem": prob,
                    "from_cp": cp1_num, "to_cp": cp2_num,
                    "after_loc": loc_after, "next_before_loc": loc_next_before,
                    "rebound_pct": (loc_next_before - loc_after) / loc_after * 100,
                })

# ══════════════════════════════════════════════════════════════
# WRITE REPORT
# ══════════════════════════════════════════════════════════════

md = []
md.append("# Slop Analysis: Before → After Skill Deep Dive\n\n")

# Overview
md.append("## 1. Overview\n\n")
md.append(f"Analyzed **{len(all_data)} checkpoints** across Pangu review_refactor runs.\n\n")

md.append("### Test Impact Summary\n\n")
md.append("| | Count | % |\n|---|---:|---:|\n")
md.append(f"| Same | {same} | {same*100//len(all_data)}% |\n")
md.append(f"| 🟢 Improved | {len(improved)} | {len(improved)*100//len(all_data)}% |\n")
md.append(f"| 🔴 Worsened | {len(worsened)} | {len(worsened)*100//len(all_data)}% |\n\n")

# Smell analysis
md.append("## 2. Code Smell Analysis by Category\n\n")
for cat_name, metrics in smell_categories.items():
    md.append(f"### {cat_name}\n\n")
    md.append("| Metric | Mean Δ | Median Δ | Improved % | Worsened % |\n")
    md.append("|--------|--------|----------|------------|------------|\n")
    for m in metrics:
        vals = quality_deltas.get(m, [])
        if not vals: continue
        mean_d = np.mean(vals)
        med_d = np.median(vals)
        if m in ("func_count", "classes", "methods", "file_count"):
            pct_i = sum(1 for v in vals if v > 0) / len(vals) * 100
            pct_w = sum(1 for v in vals if v < 0) / len(vals) * 100
        else:
            pct_i = sum(1 for v in vals if v < 0) / len(vals) * 100
            pct_w = sum(1 for v in vals if v > 0) / len(vals) * 100
        md.append(f"| {m} | {mean_d:+.3f} | {med_d:+.1f} | {pct_i:.0f}% | {pct_w:.0f}% |\n")
    md.append("\n")

# Source code changes
md.append("## 3. Source Code Changes\n\n")
loc_nets = [e["diff"]["net"] for e in all_data if e["diff"]]
loc_adds = [e["diff"]["added"] for e in all_data if e["diff"]]
loc_rems = [e["diff"]["removed"] for e in all_data if e["diff"]]
if loc_nets:
    md.append(f"- **Checkpoints analyzed**: {len(loc_nets)}\n")
    md.append(f"- **No code change**: {no_change_count} ({no_change_count*100//len(loc_nets)}%)\n")
    md.append(f"- **Small change (≤20 lines)**: {small_change_count} ({small_change_count*100//len(loc_nets)}%)\n")
    md.append(f"- **Large change (>20 lines)**: {large_change_count} ({large_change_count*100//len(loc_nets)}%)\n")
    md.append(f"- **Mean net LOC change**: {np.mean(loc_nets):+.1f}\n")
    md.append(f"- **Total lines removed**: {sum(loc_rems)}\n")
    md.append(f"- **Total lines added**: {sum(loc_adds)}\n\n")

# Skill agent behavior
md.append("## 4. Skill Agent Behavior\n\n")
md.append("### Tool Usage\n\n")
md.append("| Tool | Count |\n|------|------:|\n")
for tool, cnt in sorted(skill_edits.items(), key=lambda x: -x[1]):
    md.append(f"| {tool} | {cnt} |\n")
md.append(f"\n- Mean turns per skill run: {np.mean(skill_turns):.1f}\n")
md.append(f"- Mean tool calls per skill run: {np.mean(skill_tool_calls):.1f}\n\n")

# Erosion/Verbosity trajectory
md.append("## 5. Erosion & Verbosity Trajectories\n\n")
md.append("![Erosion Trajectory](slop-erosion-trajectory.png)\n\n")
md.append("![Verbosity Trajectory](slop-verbosity-trajectory.png)\n\n")

md.append("### Erosion by Phase\n\n")
md.append("| Phase | Baseline | Skill (Before) | Skill (After) |\n")
md.append("|-------|----------|----------------|---------------|\n")
for b in bins_list:
    bl_e = np.mean(bl_erosion_bins[b]) if bl_erosion_bins[b] else 0
    sb_e = np.mean(sk_erosion_before_bins[b]) if sk_erosion_before_bins[b] else 0
    sa_e = np.mean(sk_erosion_after_bins[b]) if sk_erosion_after_bins[b] else 0
    md.append(f"| {bin_labels[b]} | {bl_e:.3f} | {sb_e:.3f} | {sa_e:.3f} |\n")

# Slop rebound
md.append("\n## 6. Slop Rebound Analysis\n\n")
md.append(f"Cases where LOC increased >10% between after-skill and next checkpoint's before-skill: **{len(rebound_cases)}**\n\n")
if rebound_cases:
    md.append("| Problem | CP → CP | After LOC | Next Before LOC | Rebound |\n")
    md.append("|---------|---------|-----------|-----------------|--------|\n")
    for rc in sorted(rebound_cases, key=lambda x: -x["rebound_pct"])[:10]:
        md.append(f"| {rc['problem']} | cp{rc['from_cp']}→cp{rc['to_cp']} | {rc['after_loc']} | {rc['next_before_loc']} | +{rc['rebound_pct']:.0f}% |\n")

# Cost analysis
md.append("\n## 7. Cost-Benefit Analysis\n\n")
if skill_costs:
    md.append(f"- Total skill cost: ${total_skill_cost:.2f}\n")
    md.append(f"- Tests gained: +{total_test_gain}\n")
    md.append(f"- Tests lost: {total_test_loss}\n")
    md.append(f"- Net test change: {total_test_gain + total_test_loss:+d}\n")
    if total_skill_cost > 0:
        md.append(f"- Cost per net test gained: ${total_skill_cost / max(1, total_test_gain + total_test_loss):.2f}\n")

# Failure modes
md.append("\n## 8. Failure Mode Analysis\n\n")
md.append(f"Total regressions: {len(worsened)}\n\n")
for mode, cases in sorted(failure_modes.items()):
    md.append(f"- **{mode}**: {len(cases)} cases\n")
    for c in cases[:3]:
        md.append(f"  - {c['problem']}/{c['checkpoint']}: tests {c['test_delta']:+d}, LOC {c['diff']['net'] if c['diff'] else '?':+d}\n")
md.append("\n")

# Case studies
md.append("## 9. Case Studies\n\n")
md.append("### Top Improvements\n\n")
md.append("| Problem/CP | Tests Δ | LOC Δ | Added | Removed |\n")
md.append("|------------|---------|-------|-------|--------|\n")
for e in sorted(improved, key=lambda x: -x["test_delta"])[:5]:
    d = e["diff"]
    md.append(f"| {e['problem']}/{e['checkpoint']} | +{e['test_delta']} | {d['net'] if d else '?':+d} | +{d['added'] if d else '?'} | -{d['removed'] if d else '?'} |\n")

md.append("\n### Top Regressions\n\n")
md.append("| Problem/CP | Tests Δ | LOC Δ | Added | Removed |\n")
md.append("|------------|---------|-------|-------|--------|\n")
for e in sorted(worsened, key=lambda x: x["test_delta"])[:5]:
    d = e["diff"]
    md.append(f"| {e['problem']}/{e['checkpoint']} | {e['test_delta']} | {d['net'] if d else '?':+d} | +{d['added'] if d else '?'} | -{d['removed'] if d else '?'} |\n")

# Charts
md.append("\n## Charts\n\n")
md.append("![Quality Deltas](slop-quality-deltas.png)\n\n")
md.append("![LOC Distribution](slop-loc-distribution.png)\n\n")
md.append("![Test vs Change](slop-test-vs-change.png)\n\n")
md.append("![Smell Categories](slop-smell-categories.png)\n\n")

with open(f"{OUT}/slop-analysis.md", "w") as f:
    f.writelines(md)
print("Written slop-analysis.md")

# ══════════════════════════════════════════════════════════════
# CHARTS
# ══════════════════════════════════════════════════════════════

# Chart 1: Quality metric deltas
fig, ax = plt.subplots(figsize=(14, 7))
keys_plot = ["total_lines", "comments", "loc", "cc_sum", "cc_max", "cc_high_count",
             "depth_max", "clone_lines", "lint_errors", "unused_vars",
             "single_use_funcs", "verbosity_lines", "C901", "PLR0912", "PLR1702"]
labels_plot = keys_plot
means = [np.mean(quality_deltas.get(k, [0])) for k in keys_plot]
colors = ['#2ECC71' if m < -0.01 else ('#E74C3C' if m > 0.01 else '#95A5A6') for m in means]

ax.barh(range(len(labels_plot)), means, color=colors, height=0.6)
ax.set_yticks(range(len(labels_plot)))
ax.set_yticklabels(labels_plot, fontsize=9)
ax.axvline(x=0, color='black', linewidth=0.5)
ax.set_xlabel('Mean Change (After − Before Skill)', fontsize=11)
ax.set_title('Quality Metric Changes After Skill Application', fontsize=13, fontweight='bold')
for i, m in enumerate(means):
    if abs(m) > 0.01:
        ax.text(m + (0.05 if m >= 0 else -0.05), i, f'{m:+.2f}', va='center',
                ha='left' if m >= 0 else 'right', fontsize=8)
ax.grid(axis='x', alpha=0.3)
fig.tight_layout()
fig.savefig(f"{OUT}/slop-quality-deltas.png", dpi=150)
plt.close()
print("Saved slop-quality-deltas.png")

# Chart 2: LOC distribution
if loc_nets:
    fig, ax = plt.subplots(figsize=(10, 5))
    clipped = [max(-100, min(50, d)) for d in loc_nets]
    ax.hist(clipped, bins=60, color='#1F4E79', alpha=0.8, edgecolor='white')
    ax.axvline(x=0, color='red', linewidth=1, linestyle='--')
    ax.axvline(x=np.mean(loc_nets), color='green', linewidth=2, label=f'Mean: {np.mean(loc_nets):+.1f}')
    ax.axvline(x=np.median(loc_nets), color='orange', linewidth=2, linestyle=':', label=f'Median: {np.median(loc_nets):+.1f}')
    ax.set_xlabel('Net LOC Change (After − Before)', fontsize=11)
    ax.set_ylabel('Number of Checkpoints', fontsize=11)
    ax.set_title('Distribution of LOC Changes After Skill', fontsize=13, fontweight='bold')
    ax.legend(fontsize=10)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(f"{OUT}/slop-loc-distribution.png", dpi=150)
    plt.close()
    print("Saved slop-loc-distribution.png")

# Chart 3: Test delta vs change size scatter
if change_vs_test:
    fig, ax = plt.subplots(figsize=(10, 6))
    sizes = [x[0] for x in change_vs_test]
    tests = [x[1] for x in change_vs_test]
    colors_s = ['#2ECC71' if t > 0 else ('#E74C3C' if t < 0 else '#95A5A6') for t in tests]
    ax.scatter(sizes, tests, c=colors_s, alpha=0.5, s=30)
    ax.axhline(y=0, color='black', linewidth=0.5)
    ax.set_xlabel('Total Lines Changed (Added + Removed)', fontsize=11)
    ax.set_ylabel('Test Delta (After − Before)', fontsize=11)
    ax.set_title('Test Impact vs Skill Change Size', fontsize=13, fontweight='bold')
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(f"{OUT}/slop-test-vs-change.png", dpi=150)
    plt.close()
    print("Saved slop-test-vs-change.png")

# Chart 4: Smell categories radar/bar
fig, axes = plt.subplots(1, 2, figsize=(16, 6))

# Erosion trajectory
ax = axes[0]
x_labels = [bin_labels[b] for b in bins_list]
bl_e = [np.mean(bl_erosion_bins[b]) if bl_erosion_bins[b] else 0 for b in bins_list]
sk_b_e = [np.mean(sk_erosion_before_bins[b]) if sk_erosion_before_bins[b] else 0 for b in bins_list]
sk_a_e = [np.mean(sk_erosion_after_bins[b]) if sk_erosion_after_bins[b] else 0 for b in bins_list]

ax.plot(x_labels, bl_e, 's--', color='#7BA3CC', markersize=7, linewidth=1.5, label='Baseline')
ax.plot(x_labels, sk_b_e, 'o-', color='#E8833A', markersize=7, linewidth=2, label='Skill (Before)')
ax.plot(x_labels, sk_a_e, '^-', color='#1F4E79', markersize=7, linewidth=2, label='Skill (After)')
ax.set_ylabel('Erosion (CC Concentration)', fontsize=11)
ax.set_xlabel('Problem Progress', fontsize=11)
ax.set_title('Structural Erosion Trajectory', fontsize=12, fontweight='bold')
ax.set_ylim(bottom=0)
ax.legend(fontsize=9)
ax.grid(alpha=0.3)

# Verbosity trajectory
ax = axes[1]
bl_v = [np.mean(bl_verbosity_bins[b]) if bl_verbosity_bins[b] else 0 for b in bins_list]
sk_b_v = [np.mean(sk_verbosity_before_bins[b]) if sk_verbosity_before_bins[b] else 0 for b in bins_list]
sk_a_v = [np.mean(sk_verbosity_after_bins[b]) if sk_verbosity_after_bins[b] else 0 for b in bins_list]

ax.plot(x_labels, bl_v, 's--', color='#7BA3CC', markersize=7, linewidth=1.5, label='Baseline')
ax.plot(x_labels, sk_b_v, 'o-', color='#E8833A', markersize=7, linewidth=2, label='Skill (Before)')
ax.plot(x_labels, sk_a_v, '^-', color='#1F4E79', markersize=7, linewidth=2, label='Skill (After)')
ax.set_ylabel('Verbosity (Clone + Flagged / LOC)', fontsize=11)
ax.set_xlabel('Problem Progress', fontsize=11)
ax.set_title('Verbosity Trajectory', fontsize=12, fontweight='bold')
ax.set_ylim(bottom=0)
ax.legend(fontsize=9)
ax.grid(alpha=0.3)

fig.suptitle('Erosion & Verbosity: Baseline vs Skill Run', fontsize=14, fontweight='bold', y=1.02)
fig.tight_layout()
fig.savefig(f"{OUT}/slop-erosion-trajectory.png", dpi=150, bbox_inches='tight')
plt.close()
print("Saved slop-erosion-trajectory.png")

# Chart 5: Smell category summary
fig, ax = plt.subplots(figsize=(12, 5))
cat_names = []
cat_means = []
for cat_name, metrics in smell_categories.items():
    vals = []
    for m in metrics:
        v = quality_deltas.get(m, [])
        if v:
            # Normalize: for most metrics, negative = good
            if m not in ("func_count", "classes", "methods", "file_count"):
                vals.extend([-x for x in v])  # flip so positive = improvement
            else:
                vals.extend(v)
    if vals:
        cat_names.append(cat_name.split("(")[0].strip())
        cat_means.append(np.mean(vals))

colors_cat = ['#2ECC71' if m > 0 else '#E74C3C' for m in cat_means]
ax.barh(range(len(cat_names)), cat_means, color=colors_cat, height=0.5)
ax.set_yticks(range(len(cat_names)))
ax.set_yticklabels(cat_names, fontsize=10)
ax.axvline(x=0, color='black', linewidth=0.5)
ax.set_xlabel('Mean Improvement (positive = better)', fontsize=11)
ax.set_title('Skill Impact by Code Smell Category', fontsize=13, fontweight='bold')
ax.grid(axis='x', alpha=0.3)
fig.tight_layout()
fig.savefig(f"{OUT}/slop-smell-categories.png", dpi=150)
plt.close()
print("Saved slop-smell-categories.png")

# Verbosity trajectory separate chart
fig, ax = plt.subplots(figsize=(10, 6))
ax.plot(x_labels, bl_v, 's--', color='#7BA3CC', markersize=8, linewidth=2, label='Baseline')
ax.plot(x_labels, sk_b_v, 'o-', color='#E8833A', markersize=8, linewidth=2.5, label='Skill Run (Before Cleanup)')
ax.plot(x_labels, sk_a_v, '^-', color='#1F4E79', markersize=8, linewidth=2.5, label='Skill Run (After Cleanup)')
ax.set_ylabel('Verbosity', fontsize=12)
ax.set_xlabel('Problem Progress', fontsize=12)
ax.set_title('Verbosity Trajectory: Baseline vs Skill Run', fontsize=14, fontweight='bold')
ax.set_ylim(bottom=0)
ax.legend(fontsize=11)
ax.grid(alpha=0.3)
fig.tight_layout()
fig.savefig(f"{OUT}/slop-verbosity-trajectory.png", dpi=150)
plt.close()
print("Saved slop-verbosity-trajectory.png")

print(f"\n{'='*60}")
print(f"SUMMARY")
print(f"{'='*60}")
print(f"Checkpoints: {len(all_data)}")
print(f"Test impact: {same} same ({same*100//len(all_data)}%), {len(improved)} improved, {len(worsened)} worsened")
print(f"Code changes: {no_change_count} none, {small_change_count} small, {large_change_count} large")
print(f"Rebound cases: {len(rebound_cases)}")
print(f"All outputs in {OUT}/")
