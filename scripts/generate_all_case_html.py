#!/usr/bin/env python3
"""Generate per-problem HTML analysis for all Pangu review_refactor problems.
Shows before→after skill diff with slop annotations and test impact analysis.

Usage: python generate_all_case_html.py
"""
import sys
sys.path.insert(0, "/shared_workspace_mfs/ximing/slop-code-bench/scripts")
from generate_problem_html import generate_problem_html

# All Pangu review_refactor runs and their problems
pangu_runs = {
    "outputs/pangu/claude_code-2.0.51_review_refactor_20260602T0203": [
        "cfgpipe", "code_search", "dag_execution", "dynamic_buffer",
        "etl_pipeline", "eve_industry", "eve_jump_planner"
    ],
    "outputs/pangu/claude_code-2.0.51_review_refactor_20260603T2011": [
        "database_migration", "datagate"
    ],
    "outputs/pangu/claude_code-2.0.51_review_refactor_20260604T1409": [
        "circuit_eval", "env_manager", "eve_route_planner"
    ],
    "outputs/pangu/claude_code-2.0.51_review_refactor_20260605T1442": [
        "file_query_tool", "log_query", "migrate_configs", "mvvault", "pwd_manager"
    ],
    "outputs/pangu/claude_code-2.0.51_review_refactor_20260608T1615": [
        "file_backup", "rejector"
    ],
    "outputs/pangu/claude_code-2.0.51_review_refactor_20260608T1902": [
        "dynamic_config_service_api", "file_merger"
    ],
    "outputs/pangu/claude_code-2.0.51_review_refactor_20260610T0303": [
        "eve_market_tools", "forge", "l2m", "meshctl", "sith", "textdrop", "xjq"
    ],
    "outputs/pangu/claude_code-2.0.51_review_refactor_20260610T1405": [
        "mocked_http", "recli", "trajectory_api"
    ],
    "outputs/pangu/claude_code-2.0.51_review_refactor_20260610T1527": [
        "sheeteval", "test_translator"
    ],
    "outputs/pangu/claude_code-2.0.51_review_refactor_20260611T0157": [
        "sith"  # duplicate, will be skipped if already generated
    ],
}

import os
os.chdir("/shared_workspace_mfs/ximing/slop-code-bench")
os.makedirs("docs/report/cases", exist_ok=True)

generated = set()
for run_dir, problems in pangu_runs.items():
    for prob in problems:
        if prob in generated:
            continue
        prob_dir = f"{run_dir}/{prob}"
        if not os.path.isdir(prob_dir):
            print(f"SKIP {prob}: {prob_dir} not found")
            continue
        output = f"docs/report/cases/pangu-{prob}.html"
        try:
            generate_problem_html(prob_dir, prob, output)
            generated.add(prob)
        except Exception as e:
            print(f"ERROR {prob}: {e}")

print(f"\nDone! Generated {len(generated)} HTML files in docs/report/cases/")

# Generate index page
with open("docs/report/cases/index.html", "w") as f:
    f.write('''<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Pangu Slop Analysis — All Problems</title>
<style>
body { font-family: -apple-system, sans-serif; background: #0d1117; color: #c9d1d9; padding: 20px; }
h1 { border-bottom: 2px solid #30363d; padding-bottom: 10px; }
a { color: #58a6ff; text-decoration: none; }
a:hover { text-decoration: underline; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; }
.card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; }
.card:hover { border-color: #58a6ff; }
</style></head><body>
<h1>Pangu Slop Analysis — All Problems</h1>
<div class="grid">
''')
    for prob in sorted(generated):
        f.write(f'<a href="pangu-{prob}.html" class="card">{prob}</a>\n')
    f.write('</div></body></html>')

print("Generated index.html")
