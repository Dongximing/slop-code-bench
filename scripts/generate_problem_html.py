#!/usr/bin/env python3
"""Generate per-problem HTML with function-level AST analysis and code diffs.
Matches the style of skill_diff_viewer.html.

Usage:
    python generate_problem_html.py <problem_dir> <problem_name> [output_path]
"""
import json, os, glob, sys, difflib, html as html_mod
from collections import defaultdict

PATTERNS = [
    ("if-statements", lambda s: s.get("control_flow", 0)),
    ("branches", lambda s: s.get("branches", 0)),
    ("nesting depth", lambda s: s.get("max_nesting_depth", 0)),
    ("comparisons", lambda s: s.get("comparisons", 0)),
    ("exception handlers", lambda s: s.get("exception_scaffold", 0)),
    ("statements", lambda s: s.get("statements", 0)),
    ("expressions", lambda s: s.get("expressions_total", 0)),
]

def read_json(p):
    try:
        with open(p) as f: return json.load(f)
    except: return None

def read_jsonl(p):
    results = []
    try:
        with open(p) as f:
            for line in f:
                line = line.strip()
                if line:
                    results.append(json.loads(line))
    except: pass
    return results

def count_tests(ef):
    d = read_json(ef)
    if not d: return 0, 0
    p = sum(len(g.get('passed',[])) for g in d.get('tests',{}).values())
    f = sum(len(g.get('failed',[])) for g in d.get('tests',{}).values())
    return p, f

def fn_key(sym):
    return (sym.get("file_path",""), sym.get("parent_class",""), sym.get("name",""))

def generate_diff_html(before_file, after_file):
    try:
        with open(before_file) as f: b = f.readlines()
        with open(after_file) as f: a = f.readlines()
    except: return ""
    diff = list(difflib.unified_diff(b, a, lineterm=''))
    if len(diff) <= 2: return ""
    parts = []
    for line in diff[2:]:
        escaped = html_mod.escape(line)
        if line.startswith('@@'):
            parts.append(f'<div class="dl hunk">{escaped}</div>')
        elif line.startswith('+'):
            parts.append(f'<div class="dl add">{escaped}</div>')
        elif line.startswith('-'):
            parts.append(f'<div class="dl del">{escaped}</div>')
        else:
            parts.append(f'<div class="dl ctx">{escaped}</div>')
    return '<div class="diff">' + ''.join(parts) + '</div>'

def fn_diff_html(before_lines, after_lines):
    """Generate diff for a specific function's lines."""
    diff = list(difflib.unified_diff(before_lines, after_lines, lineterm=''))
    if len(diff) <= 2: return ""
    parts = []
    for line in diff[2:]:
        escaped = html_mod.escape(line)
        if line.startswith('@@'):
            parts.append(f'<div class="dl hunk">{escaped}</div>')
        elif line.startswith('+'):
            parts.append(f'<div class="dl add">{escaped}</div>')
        elif line.startswith('-'):
            parts.append(f'<div class="dl del">{escaped}</div>')
        else:
            parts.append(f'<div class="dl ctx">{escaped}</div>')
    return '<div class="diff">' + ''.join(parts) + '</div>'

def extract_fn_lines(filepath, start, end):
    try:
        with open(filepath) as f:
            lines = f.readlines()
        return lines[start-1:end]
    except:
        return []

def generate_problem_html(problem_dir, problem_name, output_path):
    checkpoints = sorted(glob.glob(f"{problem_dir}/checkpoint_*/"))

    # Collect all function data
    all_functions = []  # (cp, fn_key, before_sym, after_sym, diff_html)
    cp_summaries = []
    pattern_totals = defaultdict(lambda: {"improved": 0, "regressed": 0, "unchanged": 0, "net": 0})

    for cp_dir in checkpoints:
        cpname = os.path.basename(cp_dir.rstrip('/'))

        # Tests
        bt_p, bt_f = count_tests(f"{cp_dir}evaluation.json")
        at_p, at_f = count_tests(f"{cp_dir}after_skill/evaluation.json")

        # Quality
        bq = read_json(f"{cp_dir}quality_analysis/overall_quality.json") or {}
        aq = read_json(f"{cp_dir}after_skill/quality_analysis/overall_quality.json") or {}

        # Symbols (functions)
        b_syms = read_jsonl(f"{cp_dir}quality_analysis/symbols.jsonl")
        a_syms = read_jsonl(f"{cp_dir}after_skill/quality_analysis/symbols.jsonl")

        # Trajectory
        traj = read_json(f"{cp_dir}after_skill/trajectory_stats_after.json") or {}

        # LOC
        b_pys = sorted(glob.glob(f"{cp_dir}snapshot/*.py"))
        a_pys = sorted(glob.glob(f"{cp_dir}after_skill/snapshot/*.py"))
        b_loc = sum(sum(1 for _ in open(f)) for f in b_pys) if b_pys else 0
        a_loc = sum(sum(1 for _ in open(f)) for f in a_pys) if a_pys else 0

        # File-level diff
        b_map = {os.path.basename(f): f for f in b_pys}
        a_map = {os.path.basename(f): f for f in a_pys}
        file_diffs = {}
        for fname in sorted(set(list(b_map.keys()) + list(a_map.keys()))):
            if fname in b_map and fname in a_map:
                dh = generate_diff_html(b_map[fname], a_map[fname])
                if dh:
                    file_diffs[fname] = dh

        # Match functions by key
        b_fn_map = {fn_key(s): s for s in b_syms if s.get("type") in ("function", "method")}
        a_fn_map = {fn_key(s): s for s in a_syms if s.get("type") in ("function", "method")}

        matched_fns = []
        for key in sorted(set(list(b_fn_map.keys()) + list(a_fn_map.keys()))):
            bs = b_fn_map.get(key)
            as_ = a_fn_map.get(key)
            if bs and as_:
                # Compute pattern changes
                changes = {}
                for pname, pfn in PATTERNS:
                    bv = pfn(bs)
                    av = pfn(as_)
                    d = av - bv
                    changes[pname] = (bv, av, d)
                    if d < 0: pattern_totals[pname]["improved"] += 1; pattern_totals[pname]["net"] += d
                    elif d > 0: pattern_totals[pname]["regressed"] += 1; pattern_totals[pname]["net"] += d
                    else: pattern_totals[pname]["unchanged"] += 1

                # Get function-level diff
                fn_diff = ""
                fp = bs.get("file_path", "")
                if fp in b_map and fp in a_map:
                    b_lines = extract_fn_lines(b_map[fp], bs["start"], bs["end"])
                    a_lines = extract_fn_lines(a_map[fp], as_["start"], as_["end"])
                    fn_diff = fn_diff_html(b_lines, a_lines)

                matched_fns.append({
                    "key": key, "before": bs, "after": as_,
                    "changes": changes, "diff": fn_diff,
                    "cc_before": bs.get("complexity", 0),
                    "cc_after": as_.get("complexity", 0),
                    "sloc_before": bs.get("sloc", 0),
                    "sloc_after": as_.get("sloc", 0),
                    "changed": fn_diff != "",
                })

        cp_summaries.append({
            "name": cpname,
            "bt_p": bt_p, "bt_f": bt_f, "at_p": at_p, "at_f": at_f,
            "test_delta": at_p - bt_p,
            "b_loc": b_loc, "a_loc": a_loc, "loc_delta": a_loc - b_loc,
            "bq": bq, "aq": aq, "traj": traj,
            "matched_fns": matched_fns,
            "file_diffs": file_diffs,
            "total_fns": len(b_fn_map),
            "changed_fns": sum(1 for f in matched_fns if f["changed"]),
        })

    # Count totals
    total_fns = sum(cs["total_fns"] for cs in cp_summaries)
    addressed_fns = sum(cs["changed_fns"] for cs in cp_summaries)

    # ═══ Generate HTML ═══
    h = []
    h.append(f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Slop Analysis: {problem_name}</title>
<style>
:root {{
    --bg:#0d1117; --panel:#161b22; --border:#30363d; --fg:#c9d1d9; --muted:#8b949e;
    --add-bg:#0f2d1a; --add-fg:#7ee787; --del-bg:#3d1418; --del-fg:#ffa198;
    --hunk:#1f6feb; --good:#2ea043; --bad:#da3633; --flat:#6e7681;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif; background:var(--bg); color:var(--fg); }}
header {{ padding:20px 24px; border-bottom:1px solid var(--border); background:var(--panel); }}
header h1 {{ margin:0 0 4px; font-size:20px; }}
header p {{ margin:0; color:var(--muted); font-size:13px; }}
.container {{ padding:16px 24px 60px; }}
.overview {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(200px,1fr)); gap:12px; margin:16px 0; }}
.stat {{ background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:14px 16px; }}
.stat .label {{ color:var(--muted); font-size:12px; text-transform:uppercase; }}
.stat .value {{ font-size:24px; font-weight:700; margin-top:4px; }}
.stat .value.good {{ color:var(--good); }}
.stat .value.bad {{ color:var(--bad); }}
.stat .value.flat {{ color:var(--muted); }}
.checkpoint {{ background:var(--panel); border:1px solid var(--border); border-radius:8px; margin:20px 0; overflow:hidden; }}
.cp-header {{ padding:14px 16px; border-bottom:1px solid var(--border); display:flex; align-items:center; gap:12px; flex-wrap:wrap; }}
.cp-title {{ font-size:16px; font-weight:700; }}
.badge {{ font-size:11px; padding:2px 8px; border-radius:10px; background:#21262d; color:var(--fg); border:1px solid var(--border); }}
.badge.good {{ background:rgba(46,160,67,.18); color:var(--add-fg); border-color:var(--good); }}
.badge.bad {{ background:rgba(218,54,51,.18); color:var(--del-fg); border-color:var(--bad); }}
.badge.flat {{ color:var(--muted); }}
.cp-body {{ padding:16px; }}
.summary {{ border:1px solid var(--border); border-radius:8px; background:var(--bg); padding:14px 16px; margin-bottom:16px; }}
.summary h3 {{ margin:0 0 8px; font-size:14px; }}
.sumtbl {{ width:100%; font-size:12px; border-collapse:collapse; }}
.sumtbl th, .sumtbl td {{ text-align:left; padding:4px 10px; border-bottom:1px solid var(--border); }}
.sumtbl th {{ color:var(--muted); font-weight:600; }}
td.good {{ color:var(--add-fg); }}
td.bad {{ color:var(--del-fg); }}
td.flat {{ color:var(--muted); }}
details.card {{ border:1px solid var(--border); border-radius:8px; margin-bottom:8px; background:var(--bg); overflow:hidden; }}
details.card > summary {{ cursor:pointer; padding:10px 12px; display:flex; align-items:center; gap:10px; flex-wrap:wrap; list-style:none; }}
details.card > summary::-webkit-details-marker {{ display:none; }}
details.card > summary:hover {{ background:#11161d; }}
.sev {{ flex:none; min-width:36px; text-align:center; font-weight:700; font-size:14px; color:#fff;
        background:linear-gradient(135deg,#da3633,#9e1c1c); padding:5px 7px; border-radius:6px; }}
.sev.low {{ background:linear-gradient(135deg,#bb8009,#7a5300); }}
.sev.ok {{ background:linear-gradient(135deg,#2ea043,#196c2e); }}
.title code {{ font-size:13px; color:#79c0ff; }}
.meta {{ color:var(--muted); font-size:12px; }}
.detail {{ border-top:1px solid var(--border); padding:12px; }}
.pattbl {{ width:100%; max-width:500px; font-size:12px; margin:0 0 12px; border-collapse:collapse; }}
.pattbl th, .pattbl td {{ text-align:left; padding:3px 10px; border-bottom:1px solid var(--border); }}
.pattbl th {{ color:var(--muted); font-weight:600; }}
.diff {{ font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:12px;
         background:#010409; border:1px solid var(--border); border-radius:6px; overflow:auto; max-height:520px; }}
.dl {{ white-space:pre; padding:0 10px; }}
.dl.add {{ background:var(--add-bg); color:var(--add-fg); }}
.dl.del {{ background:var(--del-bg); color:var(--del-fg); }}
.dl.hunk {{ color:var(--hunk); background:#0d1117; }}
.dl.ctx {{ color:var(--fg); }}
.no-change {{ color:var(--muted); font-style:italic; padding:15px; text-align:center; }}
.file-section {{ margin:12px 0; }}
.file-header {{ background:#343a40; color:white; padding:6px 12px; font-family:monospace; font-size:12px; border-radius:6px 6px 0 0; }}
</style>
</head>
<body>
<header>
<h1>Slop Analysis: {problem_name}</h1>
<p>Function-level AST analysis. <b>before</b> = <code>snapshot/</code> · <b>after</b> = <code>after_skill/snapshot/</code>. Severity = cyclomatic complexity.</p>
</header>
<div class="container">
''')

    # Overview stats
    total_tests_delta = sum(cs["test_delta"] for cs in cp_summaries)
    total_loc_delta = sum(cs["loc_delta"] for cs in cp_summaries)
    h.append(f'''<div class="overview">
<div class="stat"><div class="label">Checkpoints</div><div class="value">{len(cp_summaries)}</div></div>
<div class="stat"><div class="label">Functions Tracked</div><div class="value">{total_fns}</div></div>
<div class="stat"><div class="label">Functions Changed</div><div class="value">{addressed_fns}</div></div>
<div class="stat"><div class="label">Net Test Δ</div><div class="value {"good" if total_tests_delta > 0 else "bad" if total_tests_delta < 0 else "flat"}">{total_tests_delta:+d}</div></div>
<div class="stat"><div class="label">Net LOC Δ</div><div class="value {"good" if total_loc_delta < 0 else "flat"}">{total_loc_delta:+d}</div></div>
</div>
''')

    # Pattern impact summary
    h.append('<div class="summary"><h3>Pattern impact across all matched functions</h3>')
    h.append('<table class="sumtbl"><thead><tr><th>pattern</th><th class="good">↓ improved</th><th class="bad">↑ regressed</th><th>unchanged</th><th>net Δ</th></tr></thead><tbody>')
    for pname, _ in PATTERNS:
        pt = pattern_totals[pname]
        net_cls = "good" if pt["net"] < 0 else ("bad" if pt["net"] > 0 else "flat")
        h.append(f'<tr><td>{pname}</td><td class="good">{pt["improved"]}</td><td class="bad">{pt["regressed"]}</td><td class="flat">{pt["unchanged"]}</td><td class="{net_cls}">{pt["net"]:+d}</td></tr>')
    h.append('</tbody></table></div>')

    # Per checkpoint
    for cs in cp_summaries:
        td = cs["test_delta"]
        td_cls = "good" if td > 0 else ("bad" if td < 0 else "flat")
        td_icon = "🟢" if td > 0 else ("🔴" if td < 0 else "⚪")
        loc_cls = "good" if cs["loc_delta"] < 0 else "flat"

        h.append(f'<div class="checkpoint">')
        h.append(f'<div class="cp-header">')
        h.append(f'<span class="cp-title">{cs["name"]} {td_icon}</span>')
        h.append(f'<span class="badge {td_cls}">Tests: {cs["bt_p"]}/{cs["bt_p"]+cs["bt_f"]} → {cs["at_p"]}/{cs["at_p"]+cs["at_f"]} ({td:+d})</span>')
        h.append(f'<span class="badge {loc_cls}">LOC: {cs["b_loc"]} → {cs["a_loc"]} ({cs["loc_delta"]:+d})</span>')
        h.append(f'<span class="badge flat">Functions: {cs["total_fns"]} tracked, {cs["changed_fns"]} changed</span>')

        # Quality deltas
        if cs["bq"] and cs["aq"]:
            for label, path in [("CC", ("complexity","cc_sum")), ("Comments", ("lines","comments")), ("Lint", ("lint","errors"))]:
                bv = cs["bq"].get(path[0],{}).get(path[1],0)
                av = cs["aq"].get(path[0],{}).get(path[1],0)
                d = av - bv
                if d != 0:
                    cls = "good" if d < 0 else "bad"
                    h.append(f'<span class="badge {cls}">{label}: {bv}→{av} ({d:+d})</span>')

        # Skill tools
        if cs["traj"]:
            tools = cs["traj"].get("tools_used", {})
            if tools:
                tool_str = ", ".join(f"{t}:{c}" for t,c in tools.items())
                h.append(f'<span class="badge flat">Skill: {tool_str}</span>')

        h.append('</div>')  # cp-header
        h.append('<div class="cp-body">')

        # Function cards (sorted by CC desc, only changed ones first)
        changed = sorted([f for f in cs["matched_fns"] if f["changed"]], key=lambda x: -x["cc_before"])
        unchanged = sorted([f for f in cs["matched_fns"] if not f["changed"]], key=lambda x: -x["cc_before"])

        if changed:
            h.append(f'<h3 style="color:var(--add-fg)">✅ Functions addressed by skill ({len(changed)})</h3>')
            for fn in changed:
                cc_d = fn["cc_after"] - fn["cc_before"]
                cc_cls = "good" if cc_d < 0 else ("bad" if cc_d > 0 else "flat")
                sev_cls = "" if fn["cc_before"] >= 20 else ("low" if fn["cc_before"] >= 10 else "ok")

                h.append(f'<details class="card"><summary>')
                h.append(f'<span class="sev {sev_cls}">{fn["cc_before"]}</span>')
                h.append(f'<span class="title"><code>{fn["key"][2]}</code></span>')
                h.append(f'<span class="meta">{fn["key"][0]}</span>')
                h.append(f'<span class="badge {cc_cls}">CC {fn["cc_before"]}→{fn["cc_after"]} ({cc_d:+d})</span>')
                h.append(f'<span class="badge">sloc: {fn["sloc_before"]}→{fn["sloc_after"]}</span>')

                # Pattern badges
                for pname, (bv, av, d) in fn["changes"].items():
                    if d != 0:
                        cls = "good" if d < 0 else "bad"
                        h.append(f'<span class="badge {cls}">{pname}: {bv}→{av} ({d:+d})</span>')

                h.append('</summary>')
                h.append('<div class="detail">')

                # Pattern table
                h.append('<table class="pattbl"><thead><tr><th>pattern</th><th>before</th><th>after</th><th>Δ</th></tr></thead><tbody>')
                for pname, (bv, av, d) in fn["changes"].items():
                    cls = "good" if d < 0 else ("bad" if d > 0 else "flat")
                    h.append(f'<tr><td>{pname}</td><td>{bv}</td><td>{av}</td><td class="{cls}">{d:+d}</td></tr>')
                h.append('</tbody></table>')

                # Diff
                if fn["diff"]:
                    h.append(fn["diff"])

                h.append('</div></details>')

        if unchanged:
            high_cc = [f for f in unchanged if f["cc_before"] >= 10]
            if high_cc:
                h.append(f'<h3 style="color:var(--del-fg)">⚠️ High-complexity functions NOT addressed ({len(high_cc)})</h3>')
                for fn in high_cc[:10]:
                    sev_cls = "" if fn["cc_before"] >= 20 else "low"
                    h.append(f'<details class="card"><summary>')
                    h.append(f'<span class="sev {sev_cls}">{fn["cc_before"]}</span>')
                    h.append(f'<span class="title"><code>{fn["key"][2]}</code></span>')
                    h.append(f'<span class="meta">{fn["key"][0]} — CC {fn["cc_before"]}, {fn["sloc_before"]} sloc, depth {fn["changes"].get("nesting depth",(0,0,0))[0]}</span>')
                    h.append(f'<span class="badge flat">unchanged</span>')
                    h.append('</summary><div class="detail">')
                    h.append('<table class="pattbl"><thead><tr><th>pattern</th><th>value</th></tr></thead><tbody>')
                    for pname, (bv, av, d) in fn["changes"].items():
                        h.append(f'<tr><td>{pname}</td><td>{bv}</td></tr>')
                    h.append('</tbody></table>')
                    h.append('</div></details>')

        if not changed and not file_diffs:
            h.append('<div class="no-change">No code changes by skill in this checkpoint</div>')

        # File-level diffs
        if cs["file_diffs"]:
            h.append(f'<h3>File-level diffs</h3>')
            for fname, dhtml in cs["file_diffs"].items():
                h.append(f'<div class="file-section">')
                h.append(f'<div class="file-header">{fname}</div>')
                h.append(dhtml)
                h.append('</div>')

        h.append('</div></div>')  # cp-body, checkpoint

    h.append('</div></body></html>')

    with open(output_path, 'w') as f:
        f.write('\n'.join(h))
    print(f"Generated: {output_path} ({len(cp_summaries)} checkpoints, {total_fns} functions, {addressed_fns} addressed)")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python generate_problem_html.py <problem_dir> <problem_name> [output_path]")
        sys.exit(1)
    problem_dir = sys.argv[1]
    problem_name = sys.argv[2]
    output_path = sys.argv[3] if len(sys.argv) > 3 else f"docs/report/case-{problem_name}.html"
    generate_problem_html(problem_dir, problem_name, output_path)
