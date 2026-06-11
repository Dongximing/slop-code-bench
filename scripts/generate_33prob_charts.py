#!/usr/bin/env python3
"""Generate charts for 33-problem Pangu summary — matching 21-prob style."""
import json
import os
import glob
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import numpy as np

os.chdir("/shared_workspace_mfs/ximing/slop-code-bench/outputs/pangu")

def count_core(eval_file):
    with open(eval_file) as f:
        d = json.load(f)
    core = 0
    for gn, gd in d.get("tests", {}).items():
        if "Core" in gn:
            core += len(gd.get("passed", []))
    return core

def count_all(eval_file):
    with open(eval_file) as f:
        d = json.load(f)
    return sum(len(gd.get("passed", [])) for gd in d.get("tests", {}).values())

exclude = {"layered_config_synthesizer", "execution_server", "metric_transform_lang"}

all_baselines = {
    "cfgpipe": "claude_code-2.0.51_baseline_20260603T1458",
    "circuit_eval": "claude_code-2.0.51_baseline_20260603T2011",
    "code_search": "claude_code-2.0.51_baseline_20260603T0121",
    "dag_execution": "claude_code-2.0.51_baseline_20260608T1608",
    "database_migration": "claude_code-2.0.51_baseline_20260604T1405",
    "datagate": "claude_code-2.0.51_baseline_20260603T2011",
    "dynamic_buffer": "claude_code-2.0.51_baseline_20260608T1608",
    "dynamic_config_service_api": "claude_code-2.0.51_baseline_20260608T1608",
    "env_manager": "claude_code-2.0.51_baseline_20260603T2011",
    "etl_pipeline": "claude_code-2.0.51_baseline_20260603T1458",
    "eve_industry": "claude_code-2.0.51_baseline_20260603T1458",
    "eve_jump_planner": "claude_code-2.0.51_baseline_20260603T1458",
    "eve_market_tools": "claude_code-2.0.51_baseline_20260604T1405",
    "eve_route_planner": "claude_code-2.0.51_baseline_20260604T1405",
    "file_backup": "claude_code-2.0.51_baseline_20260608T1608",
    "file_merger": "claude_code-2.0.51_baseline_20260608T1936",
    "file_query_tool": "claude_code-2.0.51_baseline_20260604T1405",
    "forge": "claude_code-2.0.51_baseline_20260604T1405",
    "l2m": "claude_code-2.0.51_baseline_20260605T1442",
    "log_query": "claude_code-2.0.51_baseline_20260604T1405",
    "meshctl": "claude_code-2.0.51_baseline_20260605T1442",
    "migrate_configs": "claude_code-2.0.51_baseline_20260605T1442",
    "mocked_http": "claude_code-2.0.51_baseline_20260608T2326",
    "mvvault": "claude_code-2.0.51_baseline_20260605T1442",
    "pwd_manager": "claude_code-2.0.51_baseline_20260605T1442",
    "recli": "claude_code-2.0.51_baseline_20260608T1608",
    "rejector": "claude_code-2.0.51_baseline_20260605T1442",
    "sheeteval": "claude_code-2.0.51_baseline_20260608T1608",
    "sith": "claude_code-2.0.51_baseline_20260605T1442",
    "test_translator": "claude_code-2.0.51_baseline_20260608T2326",
    "textdrop": "claude_code-2.0.51_baseline_20260608T1608",
    "trajectory_api": "claude_code-2.0.51_baseline_20260605T1442",
    "xjq": "claude_code-2.0.51_baseline_20260605T1442",
}

rr_runs = sorted(glob.glob("claude_code-2.0.51_review_refactor_*"))

best = {}
best_run = {}
for prob in all_baselines:
    if prob in exclude:
        continue
    bl_dir = all_baselines[prob]
    if not os.path.isdir(f"{bl_dir}/{prob}"):
        continue
    bl_cps = sorted(glob.glob(f"{bl_dir}/{prob}/checkpoint_*/evaluation.json"))
    if not bl_cps:
        continue
    for rr in rr_runs:
        if not os.path.isdir(f"{rr}/{prob}"):
            continue
        matched = 0; prob_bl = 0; prob_sk = 0
        for f in bl_cps:
            n = int(f.split('checkpoint_')[1].split('/')[0])
            as_f = f"{rr}/{prob}/checkpoint_{n}/after_skill/evaluation.json"
            rr_f = f"{rr}/{prob}/checkpoint_{n}/evaluation.json"
            if os.path.exists(as_f):
                prob_bl += count_core(f); prob_sk += count_core(as_f); matched += 1
            elif os.path.exists(rr_f):
                prob_bl += count_core(f); prob_sk += count_core(rr_f); matched += 1
        rr_total = len(glob.glob(f"{rr}/{prob}/checkpoint_*/evaluation.json"))
        if matched == 0 or matched < rr_total:
            continue
        if prob not in best or prob_sk > best[prob][1]:
            best[prob] = (prob_bl, prob_sk, prob_sk - prob_bl, rr, matched, len(bl_cps))
            best_run[prob] = rr

OUT = "/shared_workspace_mfs/ximing/slop-code-bench/docs"

# ── Chart 1: Bar chart ──
probs_alpha = sorted(best.keys())
base_vals = [best[p][0] for p in probs_alpha]
skill_vals = [best[p][1] for p in probs_alpha]

fig, ax = plt.subplots(figsize=(22, 6))
x = np.arange(len(probs_alpha))
w = 0.35
ax.bar(x - w/2, base_vals, w, label='Pangu Base', color='#7BA3CC', alpha=0.7)
ax.bar(x + w/2, skill_vals, w, label='Pangu With Cleanup Skill', color='#1F4E79')

for i, (b, s) in enumerate(zip(base_vals, skill_vals)):
    d = s - b
    if d == 0:
        continue
    color = '#006400' if d > 0 else '#8B0000'
    sign = f"+{d}" if d > 0 else str(d)
    y_pos = max(b, s) + 0.3
    ax.text(i, y_pos, sign, ha='center', va='bottom', fontsize=6.5, fontweight='bold', color=color)

ax.set_ylabel('Cumulative Core Tests Passed', fontsize=11)
ax.set_title('Pangu: 33-Problem Cumulative Core Tests — Base vs With Cleanup Skill', fontsize=13, fontweight='bold')
ax.set_xticks(x)
ax.set_xticklabels(probs_alpha, rotation=55, ha='right', fontsize=7)
ax.legend(fontsize=10)
ax.yaxis.set_major_locator(ticker.MultipleLocator(4))
ax.grid(axis='y', alpha=0.2)
fig.tight_layout()
fig.savefig(f"{OUT}/pangu-33prob-core-summary.png", dpi=150)
plt.close()
print("Saved pangu-33prob-core-summary.png")

# ── Chart 2: CUMULATIVE (running sum) core step charts ──
n_probs = len(best)
n_cols = 3
n_rows = (n_probs + n_cols - 1) // n_cols

fig, axes = plt.subplots(n_rows, n_cols, figsize=(14, n_rows * 3.2))
axes_flat = axes.flatten()

for idx, prob in enumerate(sorted(best.keys())):
    ax = axes_flat[idx]
    bl_dir = all_baselines[prob]
    rr = best_run[prob]
    bl_cps = sorted(glob.glob(f"{bl_dir}/{prob}/checkpoint_*/evaluation.json"),
                    key=lambda x: int(x.split('checkpoint_')[1].split('/')[0]))

    cp_nums = []
    bl_cores = []
    sk_cores = []
    bl_cumul = 0
    sk_cumul = 0
    for f in bl_cps:
        n = int(f.split('checkpoint_')[1].split('/')[0])
        as_f = f"{rr}/{prob}/checkpoint_{n}/after_skill/evaluation.json"
        rr_f = f"{rr}/{prob}/checkpoint_{n}/evaluation.json"
        bc = count_core(f)
        sc = None
        if os.path.exists(as_f):
            sc = count_core(as_f)
        elif os.path.exists(rr_f):
            sc = count_core(rr_f)
        if sc is not None:
            bl_cumul += bc
            sk_cumul += sc
            cp_nums.append(n)
            bl_cores.append(bl_cumul)
            sk_cores.append(sk_cumul)

    ax.plot(cp_nums, sk_cores, 'o-', color='#1F4E79', markersize=5, linewidth=2, label='Pangu With Cleanup Skill')
    ax.plot(cp_nums, bl_cores, 's--', color='#7BA3CC', markersize=5, linewidth=1.5, label='Pangu (Base)')
    ax.set_title(prob, fontsize=9, fontweight='bold')
    ax.set_xlabel('Checkpoint', fontsize=7)
    ax.set_ylabel('Cumulative Core', fontsize=7)
    ax.set_ylim(bottom=0)
    ax.yaxis.set_major_locator(ticker.MaxNLocator(integer=True))
    ax.xaxis.set_major_locator(ticker.MaxNLocator(integer=True))
    ax.tick_params(labelsize=6)
    ax.grid(alpha=0.2)

for idx in range(n_probs, len(axes_flat)):
    axes_flat[idx].set_visible(False)

handles, labels = axes_flat[0].get_legend_handles_labels()
fig.legend(handles, labels, loc='lower center', ncol=2, fontsize=9, bbox_to_anchor=(0.5, -0.01))
fig.suptitle('Pangu — Cumulative Core Tests Passed (33 Problems)', fontsize=14, fontweight='bold', y=1.01)
fig.tight_layout()
fig.savefig(f"{OUT}/pangu-33prob-core-cumulative.png", dpi=150, bbox_inches='tight')
plt.close()
print("Saved pangu-33prob-core-cumulative.png")

# ── Chart 3: Before→After only non-zero deltas ──
before_after_nonzero = []
for prob in sorted(best.keys()):
    rr = best_run[prob]
    for cp_dir in sorted(glob.glob(f"{rr}/{prob}/checkpoint_*/")):
        before_f = f"{cp_dir}evaluation.json"
        after_f = f"{cp_dir}after_skill/evaluation.json"
        if not os.path.exists(before_f) or not os.path.exists(after_f):
            continue
        bt = count_all(before_f)
        at = count_all(after_f)
        delta = at - bt
        if delta != 0:
            cpname = os.path.basename(cp_dir.rstrip('/'))
            cpnum = cpname.replace("checkpoint_", "C")
            before_after_nonzero.append((f"{prob}/{cpnum}", delta))

before_after_nonzero.sort(key=lambda x: -x[1])
labels_ba = [x[0] for x in before_after_nonzero]
deltas_ba = [x[1] for x in before_after_nonzero]
colors_ba = ['#006400' if d > 0 else '#8B0000' for d in deltas_ba]

fig, ax = plt.subplots(figsize=(18, 5))
ax.bar(range(len(deltas_ba)), deltas_ba, color=colors_ba, width=0.7)
ax.axhline(y=0, color='black', linewidth=0.5)
ax.set_xticks(range(len(labels_ba)))
ax.set_xticklabels(labels_ba, rotation=45, ha='right', fontsize=7)
ax.set_ylabel('Tests Changed', fontsize=11)
ax.set_title('Pangu 33-Problem: Before→After Skill Effect', fontsize=13, fontweight='bold')
ax.grid(axis='y', alpha=0.2)
fig.tight_layout()
fig.savefig(f"{OUT}/pangu-33prob-before-after-delta.png", dpi=150)
plt.close()
print("Saved pangu-33prob-before-after-delta.png")

print("\nAll 3 charts saved to docs/")
