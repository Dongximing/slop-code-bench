#!/usr/bin/env python3
"""Generate HTML case study with line-by-line diff and slop annotations."""
import json
import os
import glob
import difflib
import html
import sys
import re

def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except:
        return None

def count_tests(eval_file):
    d = read_json(eval_file)
    if not d: return 0, 0
    p = sum(len(g.get('passed',[])) for g in d.get('tests',{}).values())
    f = sum(len(g.get('failed',[])) for g in d.get('tests',{}).values())
    return p, f

def classify_removed_line(line):
    """Classify a removed line into slop categories."""
    stripped = line.strip()

    # Comment slop
    if stripped.startswith('#') and not stripped.startswith('#!'):
        # Check if it's a "restating" comment
        if any(kw in stripped.lower() for kw in [
            'check ', 'try to', 'parse', 'validate', 'get the', 'set the',
            'return', 'create', 'initialize', 'should never', 'we need',
            'this is', 'the following', 'note:', 'todo:', 'fixme:',
            'ensure', 'make sure', 're-encode', "python's", 'but we',
        ]):
            return "comment-slop", "Restating comment — repeats what the code does"
        if stripped == '#':
            return "comment-slop", "Empty comment line"
        return "comment-slop", "Comment removed"

    # Docstring lines
    if stripped.startswith('"""') or stripped.startswith("'''") or stripped.endswith('"""') or stripped.endswith("'''"):
        return "docstring", "Docstring line"

    # Blank line
    if not stripped:
        return "blank", "Blank line removed"

    # Unused import
    if stripped.startswith('import ') or stripped.startswith('from '):
        return "dead-code", "Potentially unused import (F401)"

    # Dead code patterns
    if stripped.startswith('pass') and len(stripped) <= 4:
        return "dead-code", "Unnecessary pass statement"

    # Redundant else after return
    if stripped in ('else:', 'elif'):
        return "style", "Potentially redundant else/elif after return"

    # Default: structural change
    return "structural", "Code removed"

def classify_added_line(line):
    stripped = line.strip()
    if not stripped:
        return "blank", "Blank line added"
    if stripped.startswith('#'):
        return "comment", "Comment added"
    return "structural", "Code added"

def generate_diff_html(before_file, after_file):
    """Generate side-by-side diff HTML with slop annotations."""
    with open(before_file) as f:
        before_lines = f.readlines()
    with open(after_file) as f:
        after_lines = f.readlines()

    diff = list(difflib.unified_diff(before_lines, after_lines,
                                      fromfile='Before Skill', tofile='After Skill',
                                      lineterm=''))

    if not diff or all(not l.startswith('+') and not l.startswith('-') for l in diff[2:]):
        return None, {}  # No changes

    # Parse unified diff into hunks
    hunks = []
    current_hunk = None
    stats = {"comment-slop": 0, "docstring": 0, "blank": 0, "dead-code": 0,
             "style": 0, "structural": 0, "comment": 0}

    for line in diff:
        if line.startswith('@@'):
            if current_hunk:
                hunks.append(current_hunk)
            # Parse hunk header
            match = re.match(r'@@ -(\d+),?\d* \+(\d+),?\d* @@(.*)', line)
            if match:
                current_hunk = {
                    "before_start": int(match.group(1)),
                    "after_start": int(match.group(2)),
                    "context": match.group(3).strip(),
                    "lines": []
                }
        elif current_hunk is not None:
            if line.startswith('-') and not line.startswith('---'):
                cat, desc = classify_removed_line(line[1:])
                stats[cat] = stats.get(cat, 0) + 1
                current_hunk["lines"].append(("removed", line[1:], cat, desc))
            elif line.startswith('+') and not line.startswith('+++'):
                cat, desc = classify_added_line(line[1:])
                stats[cat] = stats.get(cat, 0) + 1
                current_hunk["lines"].append(("added", line[1:], cat, desc))
            elif not line.startswith('---') and not line.startswith('+++'):
                current_hunk["lines"].append(("context", line[1:] if line.startswith(' ') else line, "", ""))

    if current_hunk:
        hunks.append(current_hunk)

    # Generate HTML
    html_parts = []
    for hunk in hunks:
        html_parts.append(f'<div class="hunk">')
        html_parts.append(f'<div class="hunk-header">@@ Line {hunk["before_start"]} → {hunk["after_start"]} @@ {html.escape(hunk["context"])}</div>')

        b_line = hunk["before_start"]
        a_line = hunk["after_start"]

        for status, content, cat, desc in hunk["lines"]:
            escaped = html.escape(content.rstrip('\n'))
            if status == "removed":
                color_class = {
                    "comment-slop": "slop-comment",
                    "docstring": "slop-docstring",
                    "blank": "slop-blank",
                    "dead-code": "slop-dead",
                    "style": "slop-style",
                    "structural": "slop-structural",
                }.get(cat, "slop-structural")
                tooltip = html.escape(desc)
                html_parts.append(
                    f'<div class="diff-line removed {color_class}" title="{tooltip}">'
                    f'<span class="line-num">{b_line}</span>'
                    f'<span class="diff-marker">−</span>'
                    f'<span class="diff-content">{escaped}</span>'
                    f'<span class="slop-tag">{cat}</span>'
                    f'</div>'
                )
                b_line += 1
            elif status == "added":
                html_parts.append(
                    f'<div class="diff-line added">'
                    f'<span class="line-num">{a_line}</span>'
                    f'<span class="diff-marker">+</span>'
                    f'<span class="diff-content">{escaped}</span>'
                    f'</div>'
                )
                a_line += 1
            else:
                html_parts.append(
                    f'<div class="diff-line context">'
                    f'<span class="line-num">{b_line}</span>'
                    f'<span class="diff-marker"> </span>'
                    f'<span class="diff-content">{escaped}</span>'
                    f'</div>'
                )
                b_line += 1
                a_line += 1

        html_parts.append('</div>')

    return '\n'.join(html_parts), stats


def generate_case_html(problem_dir, problem_name, output_path):
    """Generate full HTML report for a problem."""

    checkpoints = sorted(glob.glob(f"{problem_dir}/checkpoint_*/"))

    # Collect data per checkpoint
    cp_data = []
    for cp_dir in checkpoints:
        cpname = os.path.basename(cp_dir.rstrip('/'))
        cp_num = int(cpname.split('_')[1])

        bt_p, bt_f = count_tests(f"{cp_dir}evaluation.json")
        at_p, at_f = count_tests(f"{cp_dir}after_skill/evaluation.json")

        bq = read_json(f"{cp_dir}quality_analysis/overall_quality.json") or {}
        aq = read_json(f"{cp_dir}after_skill/quality_analysis/overall_quality.json") or {}
        traj = read_json(f"{cp_dir}after_skill/trajectory_stats_after.json") or {}

        # Diff each .py file
        b_pys = sorted(glob.glob(f"{cp_dir}snapshot/*.py"))
        a_pys = sorted(glob.glob(f"{cp_dir}after_skill/snapshot/*.py"))

        file_diffs = []
        total_stats = {}

        b_map = {os.path.basename(f): f for f in b_pys}
        a_map = {os.path.basename(f): f for f in a_pys}

        for fname in sorted(set(list(b_map.keys()) + list(a_map.keys()))):
            if fname in b_map and fname in a_map:
                diff_html, stats = generate_diff_html(b_map[fname], a_map[fname])
                if diff_html:
                    file_diffs.append((fname, diff_html, stats))
                    for k, v in stats.items():
                        total_stats[k] = total_stats.get(k, 0) + v

        b_loc = sum(sum(1 for _ in open(f)) for f in b_pys) if b_pys else 0
        a_loc = sum(sum(1 for _ in open(f)) for f in a_pys) if a_pys else 0

        cp_data.append({
            "name": cpname, "num": cp_num,
            "bt_p": bt_p, "bt_f": bt_f, "at_p": at_p, "at_f": at_f,
            "test_delta": at_p - bt_p,
            "b_loc": b_loc, "a_loc": a_loc, "loc_delta": a_loc - b_loc,
            "bq": bq, "aq": aq, "traj": traj,
            "file_diffs": file_diffs, "total_stats": total_stats,
        })

    # Generate HTML
    html_content = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Slop Analysis: {problem_name}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 20px; background: #f5f5f5; color: #333; }}
h1 {{ color: #1a1a2e; border-bottom: 3px solid #16213e; padding-bottom: 10px; }}
h2 {{ color: #16213e; margin-top: 30px; border-left: 4px solid #e94560; padding-left: 10px; }}
h3 {{ color: #0f3460; }}
.summary-table {{ border-collapse: collapse; width: 100%; margin: 15px 0; }}
.summary-table th, .summary-table td {{ border: 1px solid #ddd; padding: 8px 12px; text-align: center; }}
.summary-table th {{ background: #16213e; color: white; }}
.summary-table tr:nth-child(even) {{ background: #f8f9fa; }}
.improved {{ color: #27ae60; font-weight: bold; }}
.worsened {{ color: #e74c3c; font-weight: bold; }}
.unchanged {{ color: #7f8c8d; }}
.checkpoint {{ background: white; border-radius: 8px; padding: 20px; margin: 20px 0; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
.cp-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px; }}
.cp-stats {{ display: flex; gap: 20px; flex-wrap: wrap; }}
.stat-box {{ background: #f0f0f0; padding: 8px 15px; border-radius: 4px; font-size: 13px; }}
.stat-box.good {{ background: #d4edda; color: #155724; }}
.stat-box.bad {{ background: #f8d7da; color: #721c24; }}
.stat-box.neutral {{ background: #e2e3e5; color: #383d41; }}
.file-diff {{ margin: 15px 0; border: 1px solid #dee2e6; border-radius: 4px; overflow: hidden; }}
.file-header {{ background: #343a40; color: white; padding: 8px 15px; font-family: monospace; font-size: 13px; }}
.hunk {{ border-top: 1px solid #dee2e6; }}
.hunk-header {{ background: #e8f4fd; color: #0366d6; padding: 4px 15px; font-family: monospace; font-size: 12px; }}
.diff-line {{ display: flex; font-family: 'Courier New', monospace; font-size: 12px; line-height: 1.5; padding: 1px 0; }}
.diff-line.context {{ background: white; }}
.diff-line.removed {{ background: #ffeef0; }}
.diff-line.added {{ background: #e6ffed; }}
.diff-line .line-num {{ width: 45px; text-align: right; padding-right: 8px; color: #999; user-select: none; flex-shrink: 0; }}
.diff-line .diff-marker {{ width: 15px; text-align: center; flex-shrink: 0; font-weight: bold; }}
.diff-line.removed .diff-marker {{ color: #cb2431; }}
.diff-line.added .diff-marker {{ color: #22863a; }}
.diff-line .diff-content {{ flex: 1; white-space: pre-wrap; word-break: break-all; padding-right: 10px; }}
.diff-line .slop-tag {{ font-size: 10px; padding: 1px 6px; border-radius: 3px; margin-right: 5px; flex-shrink: 0; align-self: center; }}
.slop-comment .slop-tag {{ background: #fff3cd; color: #856404; }}
.slop-docstring .slop-tag {{ background: #d4edda; color: #155724; }}
.slop-blank .slop-tag {{ background: #e2e3e5; color: #383d41; }}
.slop-dead .slop-tag {{ background: #f8d7da; color: #721c24; }}
.slop-style .slop-tag {{ background: #cce5ff; color: #004085; }}
.slop-structural .slop-tag {{ background: #d6d8db; color: #1b1e21; }}
.legend {{ display: flex; gap: 12px; flex-wrap: wrap; margin: 10px 0; padding: 10px; background: white; border-radius: 4px; }}
.legend-item {{ display: flex; align-items: center; gap: 5px; font-size: 12px; }}
.legend-color {{ width: 12px; height: 12px; border-radius: 2px; }}
.no-change {{ color: #6c757d; font-style: italic; padding: 15px; text-align: center; }}
.smell-summary {{ display: flex; gap: 10px; flex-wrap: wrap; margin: 10px 0; }}
.smell-badge {{ padding: 4px 10px; border-radius: 12px; font-size: 11px; font-weight: bold; }}
</style>
</head>
<body>
<h1>Slop Analysis: {problem_name}</h1>

<div class="legend">
    <strong>Slop Categories:</strong>
    <div class="legend-item"><div class="legend-color" style="background:#fff3cd"></div> Comment Slop</div>
    <div class="legend-item"><div class="legend-color" style="background:#d4edda"></div> Docstring</div>
    <div class="legend-item"><div class="legend-color" style="background:#f8d7da"></div> Dead Code</div>
    <div class="legend-item"><div class="legend-color" style="background:#cce5ff"></div> Style</div>
    <div class="legend-item"><div class="legend-color" style="background:#d6d8db"></div> Structural</div>
    <div class="legend-item"><div class="legend-color" style="background:#e2e3e5"></div> Blank</div>
</div>

<h2>Overview</h2>
<table class="summary-table">
<tr><th>Checkpoint</th><th>Tests Before</th><th>Tests After</th><th>Δ Tests</th><th>LOC Before</th><th>LOC After</th><th>Δ LOC</th><th>Slop Found</th></tr>
'''

    for cp in cp_data:
        td = cp["test_delta"]
        if td > 0: td_class = "improved"; td_str = f"+{td}"
        elif td < 0: td_class = "worsened"; td_str = str(td)
        else: td_class = "unchanged"; td_str = "="

        slop_summary = []
        for cat, cnt in sorted(cp["total_stats"].items()):
            if cnt > 0 and cat not in ("blank", "comment"):
                slop_summary.append(f"{cat}:{cnt}")
        slop_str = ", ".join(slop_summary) if slop_summary else "none"

        html_content += f'''<tr>
<td><strong>{cp["name"]}</strong></td>
<td>{cp["bt_p"]}/{cp["bt_p"]+cp["bt_f"]}</td>
<td>{cp["at_p"]}/{cp["at_p"]+cp["at_f"]}</td>
<td class="{td_class}">{td_str}</td>
<td>{cp["b_loc"]}</td>
<td>{cp["a_loc"]}</td>
<td>{cp["loc_delta"]:+d}</td>
<td>{slop_str}</td>
</tr>'''

    html_content += '</table>\n'

    # Per-checkpoint details
    for cp in cp_data:
        td = cp["test_delta"]
        if td > 0: icon = "🟢"
        elif td < 0: icon = "🔴"
        else: icon = "⚪"

        html_content += f'''
<div class="checkpoint">
<div class="cp-header">
<h2>{cp["name"]} {icon}</h2>
</div>
<div class="cp-stats">
<div class="stat-box {"good" if td > 0 else "bad" if td < 0 else "neutral"}">Tests: {cp["bt_p"]}→{cp["at_p"]} ({td:+d})</div>
<div class="stat-box {"good" if cp["loc_delta"] < 0 else "neutral"}">LOC: {cp["b_loc"]}→{cp["a_loc"]} ({cp["loc_delta"]:+d})</div>
'''

        # Quality deltas
        if cp["bq"] and cp["aq"]:
            for label, bk, ak in [
                ("CC", "complexity", "complexity"),
                ("Comments", "lines", "lines"),
                ("Lint", "lint", "lint"),
            ]:
                if bk == "complexity":
                    bv = cp["bq"].get(bk, {}).get("cc_sum", 0)
                    av = cp["aq"].get(bk, {}).get("cc_sum", 0)
                elif bk == "lines":
                    bv = cp["bq"].get(bk, {}).get("comments", 0)
                    av = cp["aq"].get(bk, {}).get("comments", 0)
                elif bk == "lint":
                    bv = cp["bq"].get(bk, {}).get("errors", 0)
                    av = cp["aq"].get(bk, {}).get("errors", 0)
                d = av - bv
                cls = "good" if d < 0 else ("bad" if d > 0 else "neutral")
                html_content += f'<div class="stat-box {cls}">{label}: {bv}→{av} ({d:+d})</div>\n'

        # Skill agent info
        if cp["traj"]:
            tools = cp["traj"].get("tools_used", {})
            if tools:
                tool_str = ", ".join(f"{t}:{c}" for t, c in tools.items())
                html_content += f'<div class="stat-box neutral">Skill tools: {tool_str}</div>\n'

        html_content += '</div>\n'

        # Smell summary badges
        if cp["total_stats"]:
            html_content += '<div class="smell-summary">\n'
            badge_colors = {
                "comment-slop": "#fff3cd", "docstring": "#d4edda", "dead-code": "#f8d7da",
                "style": "#cce5ff", "structural": "#d6d8db", "blank": "#e2e3e5",
            }
            for cat, cnt in sorted(cp["total_stats"].items()):
                if cnt > 0:
                    bg = badge_colors.get(cat, "#e2e3e5")
                    html_content += f'<span class="smell-badge" style="background:{bg}">{cat}: {cnt} lines</span>\n'
            html_content += '</div>\n'

        # File diffs
        if cp["file_diffs"]:
            for fname, diff_html, stats in cp["file_diffs"]:
                html_content += f'<div class="file-diff">\n'
                html_content += f'<div class="file-header">{fname}</div>\n'
                html_content += diff_html
                html_content += '</div>\n'
        else:
            html_content += '<div class="no-change">No code changes in this checkpoint</div>\n'

        html_content += '</div>\n'

    html_content += '</body></html>'

    with open(output_path, 'w') as f:
        f.write(html_content)

    print(f"Generated: {output_path}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python generate_case_html.py <problem_dir> <problem_name> [output_path]")
        print("Example: python generate_case_html.py outputs/pangu/.../cfgpipe cfgpipe")
        sys.exit(1)

    problem_dir = sys.argv[1]
    problem_name = sys.argv[2]
    output_path = sys.argv[3] if len(sys.argv) > 3 else f"docs/report/case-{problem_name}.html"

    generate_case_html(problem_dir, problem_name, output_path)
