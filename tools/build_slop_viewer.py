#!/usr/bin/env python3
"""Build a self-contained HTML viewer for slop analysis (before/after skill diffs)."""

import difflib
import glob
import html
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent / "outputs" / "pangu"
OUTPUT_HTML = BASE_DIR / "slop_analysis.html"


# ---------------------------------------------------------------------------
# 1. Data extraction
# ---------------------------------------------------------------------------

def find_after_skill_dirs(base: Path) -> list[dict]:
    pairs = []
    for ad in sorted(glob.glob(str(base / "**" / "after_skill"), recursive=True)):
        ad = Path(ad)
        rel = ad.relative_to(base)
        parts = rel.parts
        if len(parts) < 4:
            continue
        run, problem, checkpoint = parts[0], parts[1], parts[2]
        parent = ad.parent
        pairs.append({
            "run": run,
            "problem": problem,
            "checkpoint": checkpoint,
            "before_quality": parent / "quality_analysis" / "overall_quality.json",
            "after_quality": ad / "quality_analysis" / "overall_quality.json",
            "before_snapshot": parent / "snapshot",
            "after_snapshot": ad / "snapshot",
            "before_symbols": parent / "quality_analysis" / "symbols.jsonl",
            "after_symbols": ad / "quality_analysis" / "symbols.jsonl",
            "before_eval": parent / "evaluation.json",
            "after_eval": ad / "evaluation.json",
        })
    return pairs


def load_json(path: Path) -> dict | None:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def load_jsonl(path: Path) -> list[dict]:
    try:
        with open(path) as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


def get_py_files(snapshot_dir: Path) -> dict[str, str]:
    files = {}
    if not snapshot_dir.exists():
        return files
    for p in sorted(snapshot_dir.rglob("*.py")):
        rel = str(p.relative_to(snapshot_dir))
        if "venv/" in rel or "site-packages/" in rel or "__pycache__" in rel:
            continue
        try:
            files[rel] = p.read_text(errors="replace")
        except Exception:
            pass
    return files


def make_unified_diff(before_text: str, after_text: str, filename: str) -> str:
    before_lines = before_text.splitlines(keepends=True)
    after_lines = after_text.splitlines(keepends=True)
    diff = difflib.unified_diff(before_lines, after_lines, fromfile=f"before/{filename}", tofile=f"after/{filename}", lineterm="")
    return "\n".join(diff)


# ---------------------------------------------------------------------------
# 2. Diff classification
# ---------------------------------------------------------------------------

COMMENT_RE = re.compile(r"^\s*#")
IMPORT_RE = re.compile(r"^\s*(import |from \S+ import )")
DOCSTRING_LINE_RE = re.compile(r'^\s*("""|\'\'\')')
PASS_RE = re.compile(r"^\s*pass\s*$")
BLANK_RE = re.compile(r"^\s*$")


def classify_hunk(removed_lines: list[str], added_lines: list[str]) -> tuple[str, str]:
    """Return (category, explanation) for a diff hunk."""
    stripped_removed = [l.lstrip("-").strip() for l in removed_lines if l.startswith("-")]
    stripped_added = [l.lstrip("+").strip() for l in added_lines if l.startswith("+")]

    if not stripped_removed and not stripped_added:
        return "no_change", ""

    all_removed_are_comments = all(
        COMMENT_RE.match(l) or BLANK_RE.match(l) or DOCSTRING_LINE_RE.match(l) or l.strip() == ""
        for l in stripped_removed
    ) if stripped_removed else False

    all_removed_are_imports = all(
        IMPORT_RE.match(l) or BLANK_RE.match(l) for l in stripped_removed
    ) if stripped_removed else False

    if stripped_removed and not stripped_added:
        if all_removed_are_comments:
            sample = next((l for l in stripped_removed if COMMENT_RE.match(l)), "")
            return "comment_slop", f"Removed {len(stripped_removed)} comment-only line(s) that restate the code — e.g. \"{sample[:60]}\""
        if all_removed_are_imports:
            imports = [l for l in stripped_removed if IMPORT_RE.match(l)]
            return "dead_code", f"Removed {len(imports)} unused import(s): {', '.join(l.strip() for l in imports[:3])}"
        if all(BLANK_RE.match(l) or PASS_RE.match(l) for l in stripped_removed):
            return "dead_code", f"Removed {len(stripped_removed)} dead line(s) (blank/pass statements)"
        return "dead_code", f"Removed {len(stripped_removed)} line(s) of unreachable or unused code"

    if stripped_removed and stripped_added:
        if all_removed_are_comments and not stripped_added:
            return "comment_slop", f"Removed {len(stripped_removed)} redundant comment(s)"

        removed_code = [l for l in stripped_removed if not COMMENT_RE.match(l) and not BLANK_RE.match(l)]
        added_code = [l for l in stripped_added if not COMMENT_RE.match(l) and not BLANK_RE.match(l)]

        only_comments_changed = not removed_code and not added_code
        if only_comments_changed:
            return "comment_slop", "Removed inline comments while keeping code unchanged"

        removed_has_if = any("if " in l for l in removed_code)
        removed_has_else = any("else:" in l or "elif " in l for l in removed_code)
        added_has_and = any(" and " in l or " or " in l for l in added_code)

        if removed_has_if and added_has_and:
            return "style", f"Merged nested if-statements into a single condition with 'and'/'or' ({len(removed_code)} → {len(added_code)} lines)"

        if removed_has_if and len(removed_code) > len(added_code) and removed_has_else:
            return "style", f"Flattened if/else chain: replaced nested else-if with elif or guard clause ({len(removed_code)} → {len(added_code)} lines)"

        removed_has_nested_if = any(re.match(r"\s{8,}if ", l) for l in removed_code)
        if removed_has_nested_if and len(removed_code) > len(added_code):
            return "style", f"Reduced nesting depth by combining conditions or extracting early returns ({len(removed_code)} → {len(added_code)} lines)"

        if len(removed_code) > len(added_code) * 1.5 and len(removed_code) > 3:
            return "duplication", f"Replaced {len(removed_code)} lines of duplicated/verbose logic with {len(added_code)} lines (possibly extracted to helper or used built-in)"

        if len(removed_code) > len(added_code):
            diff_lines = len(removed_code) - len(added_code)
            return "style", f"Simplified code: {len(removed_code)} → {len(added_code)} lines (-{diff_lines})"

    if not stripped_removed and stripped_added:
        return "structural", f"Added {len(stripped_added)} new line(s) — structural change (new helper, refactored logic)"

    return "mixed", f"Mixed change: {len(stripped_removed)} line(s) removed, {len(stripped_added)} added — multiple types of cleanup"


def classify_diff(diff_text: str) -> list[dict]:
    hunks = []
    current_hunk = None
    removed = []
    added = []

    for line in diff_text.split("\n"):
        if line.startswith("@@"):
            if current_hunk:
                category, explanation = classify_hunk(removed, added)
                hunks.append({"header": current_hunk, "category": category, "explanation": explanation, "removed": removed, "added": added})
            current_hunk = line
            removed = []
            added = []
        elif line.startswith("-") and not line.startswith("---"):
            removed.append(line)
        elif line.startswith("+") and not line.startswith("+++"):
            added.append(line)

    if current_hunk:
        category, explanation = classify_hunk(removed, added)
        hunks.append({"header": current_hunk, "category": category, "explanation": explanation, "removed": removed, "added": added})

    return hunks


def overall_classification(hunks: list[dict]) -> dict[str, int]:
    counts = defaultdict(int)
    for h in hunks:
        counts[h["category"]] += 1
    return dict(counts)


# ---------------------------------------------------------------------------
# 3. Quality metrics delta
# ---------------------------------------------------------------------------

def compute_quality_delta(before: dict, after: dict) -> dict:
    delta = {}

    key_metrics = {
        "complexity.cc_sum": ("complexity", "cc_sum"),
        "complexity.cc_max": ("complexity", "cc_max"),
        "complexity.num_complex": ("complexity", "num_complex"),
        "functions.cc_mean": ("functions", "cc_mean"),
        "functions.cc_max": ("functions", "cc_max"),
        "functions.depth_max": ("functions", "depth_max"),
        "functions.lines_mean": ("functions", "lines_mean"),
        "functions.lines_sum": ("functions", "lines_sum"),
        "functions.count": ("functions", "count"),
        "lines.loc": ("lines", "loc"),
        "lines.comments": ("lines", "comments"),
        "lines.total_lines": ("lines", "total_lines"),
        "lint.errors": ("lint", "errors"),
        "lint.fixable": ("lint", "fixable"),
        "redundancy.clone_lines": ("redundancy", "clone_lines"),
        "redundancy.clone_ratio_sum": ("redundancy", "clone_ratio_sum"),
        "waste.unused_variables": ("waste", "unused_variables"),
        "waste.single_use_functions": ("waste", "single_use_functions"),
        "waste.trivial_wrappers": ("waste", "trivial_wrappers"),
    }

    for label, (group, key) in key_metrics.items():
        bv = before.get(group, {}).get(key, 0) if before else 0
        av = after.get(group, {}).get(key, 0) if after else 0
        if isinstance(bv, (int, float)) and isinstance(av, (int, float)):
            delta[label] = {"before": bv, "after": av, "delta": round(av - bv, 4)}

    return delta


# ---------------------------------------------------------------------------
# 4. Function-level analysis
# ---------------------------------------------------------------------------

def match_functions(before_syms: list[dict], after_syms: list[dict]) -> list[dict]:
    before_map = {}
    for s in before_syms:
        if s.get("type") in ("function", "method"):
            key = (s.get("file_path", ""), s.get("parent_class", ""), s.get("name", ""))
            before_map[key] = s

    results = []
    for s in after_syms:
        if s.get("type") in ("function", "method"):
            key = (s.get("file_path", ""), s.get("parent_class", ""), s.get("name", ""))
            bs = before_map.pop(key, None)
            if bs:
                results.append({
                    "name": s["name"],
                    "file": s.get("file_path", ""),
                    "parent_class": s.get("parent_class"),
                    "before_cc": bs.get("complexity", 0),
                    "after_cc": s.get("complexity", 0),
                    "cc_delta": s.get("complexity", 0) - bs.get("complexity", 0),
                    "before_depth": bs.get("max_nesting_depth", 0),
                    "after_depth": s.get("max_nesting_depth", 0),
                    "depth_delta": s.get("max_nesting_depth", 0) - bs.get("max_nesting_depth", 0),
                    "before_sloc": bs.get("sloc", 0),
                    "after_sloc": s.get("sloc", 0),
                    "sloc_delta": s.get("sloc", 0) - bs.get("sloc", 0),
                })

    for key, bs in before_map.items():
        results.append({
            "name": bs["name"],
            "file": bs.get("file_path", ""),
            "parent_class": bs.get("parent_class"),
            "before_cc": bs.get("complexity", 0),
            "after_cc": None,
            "cc_delta": None,
            "before_depth": bs.get("max_nesting_depth", 0),
            "after_depth": None,
            "depth_delta": None,
            "before_sloc": bs.get("sloc", 0),
            "after_sloc": None,
            "sloc_delta": None,
            "removed": True,
        })

    return results


# ---------------------------------------------------------------------------
# 5. Eval comparison
# ---------------------------------------------------------------------------

def compare_evals(before_eval: dict | None, after_eval: dict | None) -> dict:
    result = {"before_passed": 0, "before_failed": 0, "after_passed": 0, "after_failed": 0, "regressions": [], "fixes": []}
    if not before_eval or not after_eval:
        return result

    before_tests = before_eval.get("tests", {})
    after_tests = after_eval.get("tests", {})

    for group_name, group_data in before_tests.items():
        result["before_passed"] += len(group_data.get("passed", []))
        result["before_failed"] += len(group_data.get("failed", []))
    for group_name, group_data in after_tests.items():
        result["after_passed"] += len(group_data.get("passed", []))
        result["after_failed"] += len(group_data.get("failed", []))

    before_passed_set = set()
    after_passed_set = set()
    for g in before_tests.values():
        before_passed_set.update(g.get("passed", []))
    for g in after_tests.values():
        after_passed_set.update(g.get("passed", []))

    before_failed_set = set()
    after_failed_set = set()
    for g in before_tests.values():
        before_failed_set.update(g.get("failed", []))
    for g in after_tests.values():
        after_failed_set.update(g.get("failed", []))

    result["regressions"] = sorted(before_passed_set & after_failed_set)
    result["fixes"] = sorted(before_failed_set & after_passed_set)

    return result


# ---------------------------------------------------------------------------
# 5b. Slop labels / colors (used by summary + HTML)
# ---------------------------------------------------------------------------

SLOP_LABELS = {
    "comment_slop": "Comment Slop",
    "dead_code": "Dead Code",
    "style": "Style Fix",
    "duplication": "Deduplication",
    "structural": "Structural",
    "mixed": "Mixed",
    "no_change": "No Change",
}

SLOP_COLORS = {
    "comment_slop": "#f0ad4e",
    "dead_code": "#d9534f",
    "style": "#5bc0de",
    "duplication": "#428bca",
    "structural": "#5cb85c",
    "mixed": "#777",
    "no_change": "#555",
}

VERDICT_LABELS = {"good": "Improved", "bad": "Regressed", "neutral": "Changed (neutral)", "no_change": "No Change"}
VERDICT_COLORS = {"good": "#2ea043", "bad": "#da3633", "neutral": "#6e7681", "no_change": "#444"}


# ---------------------------------------------------------------------------
# 5c. Case summary generator
# ---------------------------------------------------------------------------

def generate_case_summary(slop_cls: dict, quality_delta: dict, eval_cmp: dict, verdict: str) -> str:
    parts = []

    total_hunks = sum(slop_cls.values())
    if total_hunks == 0:
        return "No code changes were made by the skill."

    top_types = sorted(slop_cls.items(), key=lambda x: -x[1])
    type_desc = []
    for cat, cnt in top_types:
        label = SLOP_LABELS.get(cat, cat)
        type_desc.append(f"{label} ({cnt} hunks)")
    parts.append(f"Skill made {total_hunks} changes: {', '.join(type_desc)}.")

    loc_d = quality_delta.get("lines.loc", {}).get("delta", 0)
    lint_d = quality_delta.get("lint.errors", {}).get("delta", 0)
    cc_d = quality_delta.get("complexity.cc_sum", {}).get("delta", 0)
    waste_d = quality_delta.get("waste.unused_variables", {}).get("delta", 0)

    improvements = []
    regressions = []
    if loc_d < 0: improvements.append(f"LOC {loc_d:+.0f}")
    elif loc_d > 0: regressions.append(f"LOC {loc_d:+.0f}")
    if lint_d < 0: improvements.append(f"lint errors {lint_d:+.0f}")
    elif lint_d > 0: regressions.append(f"lint errors {lint_d:+.0f}")
    if cc_d < 0: improvements.append(f"complexity {cc_d:+.0f}")
    elif cc_d > 0: regressions.append(f"complexity {cc_d:+.0f}")
    if waste_d < 0: improvements.append(f"unused vars {waste_d:+.0f}")
    elif waste_d > 0: regressions.append(f"unused vars {waste_d:+.0f}")

    if improvements:
        parts.append(f"Improved: {', '.join(improvements)}.")
    if regressions:
        parts.append(f"Worsened: {', '.join(regressions)}.")

    reg_tests = eval_cmp.get("regressions", [])
    fix_tests = eval_cmp.get("fixes", [])
    if reg_tests:
        parts.append(f"⚠ Broke {len(reg_tests)} test(s).")
    if fix_tests:
        parts.append(f"Fixed {len(fix_tests)} previously failing test(s).")

    return " ".join(parts)


# ---------------------------------------------------------------------------
# 6. Build all data
# ---------------------------------------------------------------------------

def build_all_data(base: Path) -> dict:
    pairs = find_after_skill_dirs(base)
    print(f"Found {len(pairs)} before/after pairs")

    all_cases = []
    for i, pair in enumerate(pairs):
        if (i + 1) % 20 == 0:
            print(f"  Processing {i+1}/{len(pairs)}...")

        before_q = load_json(pair["before_quality"])
        after_q = load_json(pair["after_quality"])
        quality_delta = compute_quality_delta(before_q, after_q)

        before_files = get_py_files(pair["before_snapshot"])
        after_files = get_py_files(pair["after_snapshot"])

        all_filenames = sorted(set(before_files.keys()) | set(after_files.keys()))
        file_diffs = []
        all_hunks_classified = defaultdict(int)
        for fname in all_filenames:
            bt = before_files.get(fname, "")
            at = after_files.get(fname, "")
            if bt == at:
                continue
            diff_text = make_unified_diff(bt, at, fname)
            if not diff_text.strip():
                continue
            hunks = classify_diff(diff_text)
            cls = overall_classification(hunks)
            for k, v in cls.items():
                all_hunks_classified[k] += v
            hunk_details = [{"category": h["category"], "explanation": h["explanation"], "header": h["header"]} for h in hunks]
            file_diffs.append({
                "file": fname,
                "diff": diff_text,
                "hunks": len(hunks),
                "classification": cls,
                "hunk_details": hunk_details,
            })

        before_syms = load_jsonl(pair["before_symbols"])
        after_syms = load_jsonl(pair["after_symbols"])
        func_changes = match_functions(before_syms, after_syms)
        func_changes_notable = [f for f in func_changes if f.get("cc_delta") and f["cc_delta"] != 0]
        func_changes_notable.sort(key=lambda x: abs(x.get("cc_delta") or 0), reverse=True)

        before_eval = load_json(pair["before_eval"])
        after_eval = load_json(pair["after_eval"])
        eval_comparison = compare_evals(before_eval, after_eval)

        has_improvement = any(v["delta"] < 0 for v in quality_delta.values() if isinstance(v.get("delta"), (int, float)))
        has_regression_tests = len(eval_comparison["regressions"]) > 0
        has_regression_quality = any(v["delta"] > 0 for k, v in quality_delta.items()
                                     if k in ("complexity.cc_sum", "lint.errors", "waste.unused_variables") and isinstance(v.get("delta"), (int, float)))

        if has_regression_tests:
            verdict = "bad"
        elif has_improvement:
            verdict = "good"
        elif any(d["diff"] for d in file_diffs):
            verdict = "neutral"
        else:
            verdict = "no_change"

        case_summary = generate_case_summary(dict(all_hunks_classified), quality_delta, eval_comparison, verdict)

        case = {
            "run": pair["run"],
            "problem": pair["problem"],
            "checkpoint": pair["checkpoint"],
            "quality_delta": quality_delta,
            "file_diffs": file_diffs,
            "slop_classification": dict(all_hunks_classified),
            "func_changes": func_changes_notable[:20],
            "eval": eval_comparison,
            "verdict": verdict,
            "summary": case_summary,
        }
        all_cases.append(case)

    problems = defaultdict(list)
    for c in all_cases:
        problems[c["problem"]].append(c)

    summary = {
        "total_cases": len(all_cases),
        "good": sum(1 for c in all_cases if c["verdict"] == "good"),
        "bad": sum(1 for c in all_cases if c["verdict"] == "bad"),
        "neutral": sum(1 for c in all_cases if c["verdict"] == "neutral"),
        "no_change": sum(1 for c in all_cases if c["verdict"] == "no_change"),
        "slop_totals": defaultdict(int),
        "metric_deltas": defaultdict(lambda: {"sum": 0, "count": 0}),
    }

    for c in all_cases:
        for k, v in c["slop_classification"].items():
            summary["slop_totals"][k] += v
        for k, v in c["quality_delta"].items():
            if isinstance(v.get("delta"), (int, float)):
                summary["metric_deltas"][k]["sum"] += v["delta"]
                summary["metric_deltas"][k]["count"] += 1

    summary["slop_totals"] = dict(summary["slop_totals"])
    summary["metric_deltas"] = {k: {"avg": round(v["sum"] / max(v["count"], 1), 3), "total": round(v["sum"], 3), "count": v["count"]}
                                 for k, v in summary["metric_deltas"].items()}

    problem_summaries = {}
    for pname, cases in sorted(problems.items()):
        ps = {
            "name": pname,
            "checkpoints": len(cases),
            "good": sum(1 for c in cases if c["verdict"] == "good"),
            "bad": sum(1 for c in cases if c["verdict"] == "bad"),
            "neutral": sum(1 for c in cases if c["verdict"] == "neutral"),
            "no_change": sum(1 for c in cases if c["verdict"] == "no_change"),
            "total_loc_delta": sum(c["quality_delta"].get("lines.loc", {}).get("delta", 0) for c in cases),
            "total_lint_delta": sum(c["quality_delta"].get("lint.errors", {}).get("delta", 0) for c in cases),
            "total_cc_delta": sum(c["quality_delta"].get("complexity.cc_sum", {}).get("delta", 0) for c in cases),
            "total_waste_delta": sum(c["quality_delta"].get("waste.unused_variables", {}).get("delta", 0) for c in cases),
        }
        problem_summaries[pname] = ps

    return {
        "summary": summary,
        "problem_summaries": problem_summaries,
        "cases": all_cases,
    }


# ---------------------------------------------------------------------------
# 7. HTML generation
# ---------------------------------------------------------------------------


def esc(s):
    return html.escape(str(s))


def delta_class(v):
    if v < 0:
        return "good"
    if v > 0:
        return "bad"
    return "flat"


def render_diff_html(diff_text: str, hunk_details: list[dict] | None = None) -> str:
    hunk_map = {}
    if hunk_details:
        for hd in hunk_details:
            hunk_map[hd["header"]] = hd

    lines = []
    for raw in diff_text.split("\n"):
        escaped = esc(raw)
        if raw.startswith("@@"):
            hd = hunk_map.get(raw)
            if hd and hd.get("explanation"):
                cat = hd["category"]
                expl = esc(hd["explanation"])
                color = SLOP_COLORS.get(cat, "#777")
                label = SLOP_LABELS.get(cat, cat)
                lines.append(f'<div class="dl hunk">{escaped}</div>')
                lines.append(f'<div class="hunk-explain" style="border-left:3px solid {color}">'
                             f'<span class="hunk-tag" style="color:{color}">{label}</span> {expl}</div>')
            else:
                lines.append(f'<div class="dl hunk">{escaped}</div>')
        elif raw.startswith("---") or raw.startswith("+++"):
            lines.append(f'<div class="dl hunk">{escaped}</div>')
        elif raw.startswith("-"):
            lines.append(f'<div class="dl del">{escaped}</div>')
        elif raw.startswith("+"):
            lines.append(f'<div class="dl add">{escaped}</div>')
        else:
            lines.append(f'<div class="dl ctx">{escaped}</div>')
    return "\n".join(lines)


def build_html(data: dict) -> str:
    s = data["summary"]
    ps = data["problem_summaries"]
    cases = data["cases"]

    slop_chart_data = json.dumps(s["slop_totals"])
    metric_chart_data = json.dumps(s["metric_deltas"])

    problems_by_name = defaultdict(list)
    for c in cases:
        problems_by_name[c["problem"]].append(c)

    # Build case cards HTML
    problem_sections = []
    for pname in sorted(problems_by_name.keys()):
        pcases = problems_by_name[pname]
        psum = ps.get(pname, {})

        case_cards = []
        for c in sorted(pcases, key=lambda x: (x["run"], x["checkpoint"])):
            qd = c["quality_delta"]

            # Quality metrics table
            metric_rows = []
            for label in sorted(qd.keys()):
                m = qd[label]
                d = m.get("delta", 0)
                cls = delta_class(d)
                metric_rows.append(f'<tr><td>{esc(label)}</td><td>{m.get("before", "")}</td><td>{m.get("after", "")}</td><td class="{cls}">{d:+.2f}</td></tr>')
            metrics_html = "\n".join(metric_rows)

            # Function changes table
            func_rows = []
            for f in c.get("func_changes", [])[:15]:
                cc_d = f.get("cc_delta") or 0
                cls = delta_class(cc_d)
                name = f.get("parent_class", "") + "." + f["name"] if f.get("parent_class") else f["name"]
                func_rows.append(
                    f'<tr><td><code>{esc(name)}</code></td><td>{esc(f.get("file", ""))}</td>'
                    f'<td>{f.get("before_cc", "")}</td><td>{f.get("after_cc", "")}</td><td class="{cls}">{cc_d:+d}</td>'
                    f'<td>{f.get("before_depth", "")}</td><td>{f.get("after_depth", "")}</td>'
                    f'<td>{f.get("before_sloc", "")}</td><td>{f.get("after_sloc", "")}</td></tr>'
                )
            funcs_html = "\n".join(func_rows) if func_rows else '<tr><td colspan="9" class="empty">No function-level CC changes</td></tr>'

            # Slop classification badges
            slop_badges = []
            for cat, cnt in sorted(c.get("slop_classification", {}).items()):
                color = SLOP_COLORS.get(cat, "#777")
                label = SLOP_LABELS.get(cat, cat)
                slop_badges.append(f'<span class="slop-badge" style="border-color:{color};color:{color}">{label}: {cnt}</span>')
            slop_html = " ".join(slop_badges) if slop_badges else '<span class="empty">No diffs</span>'

            # File diffs
            diff_sections = []
            for fd in c.get("file_diffs", [])[:10]:
                diff_rendered = render_diff_html(fd["diff"], fd.get("hunk_details"))
                cls_badges = " ".join(
                    f'<span class="slop-badge" style="border-color:{SLOP_COLORS.get(k, "#777")};color:{SLOP_COLORS.get(k, "#777")}">{SLOP_LABELS.get(k, k)}: {v}</span>'
                    for k, v in fd.get("classification", {}).items()
                )
                diff_sections.append(f'''
                <details class="diff-file">
                  <summary><code>{esc(fd["file"])}</code> ({fd["hunks"]} hunks) {cls_badges}</summary>
                  <div class="diff">{diff_rendered}</div>
                </details>''')
            diffs_html = "\n".join(diff_sections) if diff_sections else '<p class="empty">No code changes</p>'

            # Eval impact
            ev = c.get("eval", {})
            eval_html = f'''
            <div class="eval-row">
              <span>Tests: {ev.get("before_passed", 0)} passed / {ev.get("before_failed", 0)} failed → {ev.get("after_passed", 0)} passed / {ev.get("after_failed", 0)} failed</span>
            </div>'''
            if ev.get("regressions"):
                eval_html += f'<div class="eval-warn">Regressions: {", ".join(ev["regressions"][:5])}{"..." if len(ev["regressions"]) > 5 else ""}</div>'
            if ev.get("fixes"):
                eval_html += f'<div class="eval-good">Fixes: {", ".join(ev["fixes"][:5])}{"..." if len(ev["fixes"]) > 5 else ""}</div>'

            verdict = c["verdict"]
            v_color = VERDICT_COLORS[verdict]
            v_label = VERDICT_LABELS[verdict]

            loc_d = qd.get("lines.loc", {}).get("delta", 0)
            lint_d = qd.get("lint.errors", {}).get("delta", 0)
            cc_d_total = qd.get("complexity.cc_sum", {}).get("delta", 0)

            case_cards.append(f'''
            <details class="case-card">
              <summary>
                <span class="verdict-dot" style="background:{v_color}" title="{v_label}"></span>
                <span class="case-title">{esc(c["run"])} / {esc(c["checkpoint"])}</span>
                <span class="badge {delta_class(loc_d)}">LOC: {loc_d:+.0f}</span>
                <span class="badge {delta_class(lint_d)}">Lint: {lint_d:+.0f}</span>
                <span class="badge {delta_class(cc_d_total)}">CC: {cc_d_total:+.0f}</span>
                {slop_html}
              </summary>
              <div class="case-detail">
                <div class="case-summary" style="background:#21262d;border-radius:6px;padding:10px 14px;margin-bottom:12px;font-size:13px;line-height:1.6">
                  {esc(c.get("summary", ""))}
                </div>
                <div class="section">
                  <h4>Quality Metrics Delta</h4>
                  <table class="metrics-tbl">
                    <thead><tr><th>Metric</th><th>Before</th><th>After</th><th>Delta</th></tr></thead>
                    <tbody>{metrics_html}</tbody>
                  </table>
                </div>
                <div class="section">
                  <h4>Function-Level Changes (by CC delta)</h4>
                  <table class="func-tbl">
                    <thead><tr><th>Function</th><th>File</th><th>CC Before</th><th>CC After</th><th>CC Δ</th><th>Depth B</th><th>Depth A</th><th>SLOC B</th><th>SLOC A</th></tr></thead>
                    <tbody>{funcs_html}</tbody>
                  </table>
                </div>
                <div class="section">
                  <h4>Test Impact</h4>
                  {eval_html}
                </div>
                <div class="section">
                  <h4>Code Diffs (classified)</h4>
                  {diffs_html}
                </div>
              </div>
            </details>''')

        cards_html = "\n".join(case_cards)
        good = psum.get("good", 0)
        bad = psum.get("bad", 0)
        neutral = psum.get("neutral", 0)
        no_ch = psum.get("no_change", 0)
        total_ckpts = psum.get("checkpoints", 0)

        problem_sections.append(f'''
        <details class="problem-group" id="problem-{esc(pname)}">
          <summary>
            <span class="problem-name">{esc(pname)}</span>
            <span class="problem-stats">
              {total_ckpts} checkpoints ·
              <span class="good">{good} improved</span> ·
              <span class="bad">{bad} regressed</span> ·
              <span class="flat">{neutral + no_ch} unchanged</span> ·
              LOC Δ {psum.get("total_loc_delta", 0):+.0f} ·
              Lint Δ {psum.get("total_lint_delta", 0):+.0f} ·
              CC Δ {psum.get("total_cc_delta", 0):+.0f}
            </span>
          </summary>
          <div class="problem-cases">
            {cards_html}
          </div>
        </details>''')

    problems_html = "\n".join(problem_sections)

    # Build verdict overview for good/bad analysis
    good_cases = [c for c in cases if c["verdict"] == "good"]
    bad_cases = [c for c in cases if c["verdict"] == "bad"]
    no_change_cases = [c for c in cases if c["verdict"] == "no_change"]

    good_list = "\n".join(
        f'<li><strong>{esc(c["problem"])}</strong> / {esc(c["checkpoint"])} ({esc(c["run"][:40])}) — '
        f'LOC {c["quality_delta"].get("lines.loc", {}).get("delta", 0):+.0f}, '
        f'Lint {c["quality_delta"].get("lint.errors", {}).get("delta", 0):+.0f}, '
        f'CC {c["quality_delta"].get("complexity.cc_sum", {}).get("delta", 0):+.0f}</li>'
        for c in sorted(good_cases, key=lambda x: x["quality_delta"].get("lint.errors", {}).get("delta", 0))[:30]
    )

    bad_list = "\n".join(
        f'<li><strong>{esc(c["problem"])}</strong> / {esc(c["checkpoint"])} ({esc(c["run"][:40])}) — '
        f'{len(c["eval"].get("regressions", []))} test regressions, '
        f'LOC {c["quality_delta"].get("lines.loc", {}).get("delta", 0):+.0f}</li>'
        for c in bad_cases[:30]
    )

    # Build Browse Changes tab — flat list of all diffs, directly visible
    changed_cases = [c for c in cases if c.get("file_diffs")]
    changed_cases.sort(key=lambda c: sum(c.get("slop_classification", {}).values()), reverse=True)

    browse_cards = []
    for c in changed_cases[:100]:
        qd = c["quality_delta"]
        verdict = c["verdict"]
        v_color = VERDICT_COLORS[verdict]
        v_label = VERDICT_LABELS[verdict]
        loc_d = qd.get("lines.loc", {}).get("delta", 0)
        lint_d = qd.get("lint.errors", {}).get("delta", 0)
        cc_d_total = qd.get("complexity.cc_sum", {}).get("delta", 0)

        file_blocks = []
        for fd in c.get("file_diffs", [])[:5]:
            diff_rendered = render_diff_html(fd["diff"], fd.get("hunk_details"))
            file_blocks.append(f'''
            <div class="browse-file">
              <div class="browse-file-name"><code>{esc(fd["file"])}</code> — {fd["hunks"]} change(s)</div>
              <div class="diff">{diff_rendered}</div>
            </div>''')
        files_html = "\n".join(file_blocks)

        browse_cards.append(f'''
        <div class="browse-card" data-verdict="{verdict}" data-problem="{esc(c["problem"])}">
          <div class="browse-header">
            <span class="verdict-dot" style="background:{v_color}" title="{v_label}"></span>
            <strong>{esc(c["problem"])}</strong> / {esc(c["checkpoint"])}
            <span class="badge {delta_class(loc_d)}">LOC {loc_d:+.0f}</span>
            <span class="badge {delta_class(lint_d)}">Lint {lint_d:+.0f}</span>
            <span class="badge {delta_class(cc_d_total)}">CC {cc_d_total:+.0f}</span>
            <span style="color:var(--muted);font-size:11px;margin-left:8px">{esc(c["run"][:50])}</span>
          </div>
          <div class="browse-summary">{esc(c.get("summary", ""))}</div>
          {files_html}
        </div>''')

    browse_html = "\n".join(browse_cards)

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Slop Analysis — Before vs After Skill</title>
<style>
:root {{
  --bg:#0d1117; --panel:#161b22; --border:#30363d; --fg:#c9d1d9; --muted:#8b949e;
  --add-bg:#0f2d1a; --add-fg:#7ee787; --del-bg:#3d1418; --del-fg:#ffa198;
  --hunk:#1f6feb; --good:#2ea043; --bad:#da3633; --flat:#6e7681;
}}
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ font:14px/1.6 -apple-system,Segoe UI,Roboto,sans-serif; background:var(--bg); color:var(--fg); }}
header {{ padding:24px 32px; border-bottom:1px solid var(--border); background:var(--panel); }}
header h1 {{ font-size:22px; margin-bottom:4px; }}
header p {{ color:var(--muted); font-size:13px; }}

/* Tabs */
.tabs {{ display:flex; gap:0; padding:0 32px; border-bottom:1px solid var(--border); background:var(--panel); }}
.tab {{ padding:12px 20px; cursor:pointer; color:var(--muted); font-weight:600; border-bottom:2px solid transparent; }}
.tab:hover {{ color:var(--fg); }}
.tab.active {{ color:var(--fg); border-bottom-color:var(--hunk); }}
.tab-panel {{ display:none; padding:24px 32px; }}
.tab-panel.active {{ display:block; }}

/* Dashboard */
.kpi-row {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; margin-bottom:24px; }}
.kpi {{ background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:16px; text-align:center; }}
.kpi .val {{ font-size:28px; font-weight:700; }}
.kpi .label {{ font-size:12px; color:var(--muted); margin-top:4px; }}
.kpi.good .val {{ color:var(--add-fg); }}
.kpi.bad .val {{ color:var(--del-fg); }}

.charts-row {{ display:grid; grid-template-columns:1fr 1fr; gap:20px; margin-bottom:24px; }}
.chart-box {{ background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:16px; }}
.chart-box h3 {{ font-size:15px; margin-bottom:12px; }}
.chart-bar {{ display:flex; align-items:center; gap:8px; margin-bottom:6px; }}
.chart-bar .bar-label {{ width:120px; font-size:12px; text-align:right; color:var(--muted); }}
.chart-bar .bar-track {{ flex:1; height:20px; background:#21262d; border-radius:4px; overflow:hidden; position:relative; }}
.chart-bar .bar-fill {{ height:100%; border-radius:4px; min-width:2px; }}
.chart-bar .bar-val {{ font-size:11px; width:60px; }}

/* Problems */
.problem-group {{ border:1px solid var(--border); border-radius:8px; margin-bottom:8px; background:var(--panel); }}
.problem-group > summary {{ cursor:pointer; padding:12px 16px; display:flex; align-items:center; gap:12px; flex-wrap:wrap; list-style:none; }}
.problem-group > summary::-webkit-details-marker {{ display:none; }}
.problem-group > summary:hover {{ background:#1c2128; }}
.problem-name {{ font-weight:700; font-size:15px; min-width:180px; }}
.problem-stats {{ font-size:12px; color:var(--muted); }}
.problem-cases {{ padding:8px 16px 16px; }}

/* Case cards */
.case-card {{ border:1px solid var(--border); border-radius:6px; margin-bottom:6px; background:var(--bg); }}
.case-card > summary {{ cursor:pointer; padding:10px 12px; display:flex; align-items:center; gap:8px; flex-wrap:wrap; list-style:none; font-size:13px; }}
.case-card > summary::-webkit-details-marker {{ display:none; }}
.case-card > summary:hover {{ background:#11161d; }}
.verdict-dot {{ width:10px; height:10px; border-radius:50%; flex:none; }}
.case-title {{ font-weight:600; min-width:200px; font-size:12px; color:var(--muted); }}
.case-detail {{ padding:12px; border-top:1px solid var(--border); }}
.section {{ margin-bottom:16px; }}
.section h4 {{ font-size:14px; margin-bottom:8px; color:var(--fg); border-bottom:1px solid var(--border); padding-bottom:4px; }}

/* Badges */
.badge {{ font-size:11px; padding:2px 8px; border-radius:10px; background:#21262d; border:1px solid var(--border); }}
.badge.good {{ color:var(--add-fg); border-color:var(--good); }}
.badge.bad {{ color:var(--del-fg); border-color:var(--bad); }}
.badge.flat {{ color:var(--muted); }}

.slop-badge {{ font-size:10px; padding:1px 6px; border-radius:8px; border:1px solid; background:transparent; margin:0 2px; }}

/* Tables */
table {{ border-collapse:collapse; width:100%; font-size:12px; }}
th, td {{ text-align:left; padding:4px 8px; border-bottom:1px solid var(--border); }}
th {{ color:var(--muted); font-weight:600; }}
td.good {{ color:var(--add-fg); }}
td.bad {{ color:var(--del-fg); }}
td.flat {{ color:var(--muted); }}
.empty {{ color:var(--muted); font-style:italic; }}

/* Diffs */
.diff-file {{ margin-bottom:6px; }}
.diff-file > summary {{ cursor:pointer; padding:6px 8px; font-size:12px; background:#21262d; border-radius:4px; }}
.diff-file > summary:hover {{ background:#2d333b; }}
.diff {{ font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:11px;
         background:#010409; border:1px solid var(--border); border-radius:4px; overflow:auto; max-height:400px; }}
.dl {{ white-space:pre; padding:0 8px; }}
.dl.add {{ background:var(--add-bg); color:var(--add-fg); }}
.dl.del {{ background:var(--del-bg); color:var(--del-fg); }}
.dl.hunk {{ color:var(--hunk); background:#0d1117; }}
.dl.ctx {{ color:var(--fg); }}

/* Hunk explanations */
.hunk-explain {{ padding:6px 12px; background:#161b22; font-size:12px; color:var(--fg); border-bottom:1px solid var(--border); }}
.hunk-tag {{ font-weight:700; font-size:11px; text-transform:uppercase; margin-right:6px; }}

/* Eval */
.eval-row {{ font-size:13px; margin-bottom:4px; }}
.eval-warn {{ color:var(--del-fg); font-size:12px; margin-top:4px; }}
.eval-good {{ color:var(--add-fg); font-size:12px; margin-top:4px; }}

/* Good/Bad analysis */
.analysis-list {{ list-style:none; padding:0; }}
.analysis-list li {{ padding:6px 10px; border-bottom:1px solid var(--border); font-size:13px; }}
.analysis-list li:hover {{ background:#21262d; }}

/* Search */
.search-box {{ margin-bottom:16px; }}
.search-box input {{ width:100%; max-width:400px; padding:8px 12px; background:#21262d; border:1px solid var(--border);
                     border-radius:6px; color:var(--fg); font-size:14px; }}
.search-box input::placeholder {{ color:var(--muted); }}

.good {{ color:var(--add-fg); }}
.bad {{ color:var(--del-fg); }}

/* Browse Changes */
.browse-controls {{ display:flex; gap:12px; margin-bottom:16px; }}
.browse-controls input {{ flex:1; max-width:300px; padding:8px 12px; background:#21262d; border:1px solid var(--border);
                          border-radius:6px; color:var(--fg); font-size:14px; }}
.browse-controls input::placeholder {{ color:var(--muted); }}
.browse-controls select {{ padding:8px 12px; background:#21262d; border:1px solid var(--border);
                           border-radius:6px; color:var(--fg); font-size:13px; }}
.browse-card {{ border:1px solid var(--border); border-radius:8px; margin-bottom:16px; background:var(--panel); overflow:hidden; }}
.browse-header {{ padding:12px 16px; display:flex; align-items:center; gap:8px; flex-wrap:wrap; font-size:14px;
                  border-bottom:1px solid var(--border); background:var(--bg); }}
.browse-summary {{ padding:8px 16px; font-size:13px; color:var(--muted); background:#161b22; border-bottom:1px solid var(--border); }}
.browse-file {{ border-top:1px solid var(--border); }}
.browse-file-name {{ padding:8px 16px; font-size:12px; font-weight:600; background:#21262d; }}
.browse-card .diff {{ max-height:600px; border:none; border-radius:0; }}
</style>
</head>
<body>
<header>
  <h1>Slop Analysis — Before vs After Skill</h1>
  <p>Analysis of {s["total_cases"]} checkpoint cases across {len(ps)} problems. "Slop" = low-quality code patterns: dead code, comment noise, style issues, duplication, structural problems.</p>
</header>

<div class="tabs">
  <div class="tab active" data-tab="browse">Browse Changes ({len(changed_cases)})</div>
  <div class="tab" data-tab="dashboard">Dashboard</div>
  <div class="tab" data-tab="problems">Problems ({len(ps)})</div>
  <div class="tab" data-tab="good-bad">Good / Bad Analysis</div>
</div>

<!-- Browse Changes -->
<div class="tab-panel active" data-tab="browse">
  <div class="browse-controls">
    <input type="text" id="browse-search" placeholder="Filter by problem name...">
    <select id="browse-filter">
      <option value="all">All verdicts</option>
      <option value="good">Improved only</option>
      <option value="bad">Regressed only</option>
      <option value="neutral">Neutral only</option>
    </select>
  </div>
  {browse_html}
</div>

<!-- Dashboard -->
<div class="tab-panel" data-tab="dashboard">
  <div class="kpi-row">
    <div class="kpi"><div class="val">{s["total_cases"]}</div><div class="label">Total Cases</div></div>
    <div class="kpi good"><div class="val">{s["good"]}</div><div class="label">Improved</div></div>
    <div class="kpi bad"><div class="val">{s["bad"]}</div><div class="label">Regressed</div></div>
    <div class="kpi"><div class="val">{s["neutral"]}</div><div class="label">Changed (neutral)</div></div>
    <div class="kpi"><div class="val">{s["no_change"]}</div><div class="label">No Change</div></div>
  </div>

  <div class="charts-row">
    <div class="chart-box">
      <h3>Slop Type Distribution (by hunk count)</h3>
      <div id="slop-chart"></div>
    </div>
    <div class="chart-box">
      <h3>Quality Metric Avg Deltas (negative = improved)</h3>
      <div id="metric-chart"></div>
    </div>
  </div>
</div>

<!-- Problems -->
<div class="tab-panel" data-tab="problems">
  <div class="search-box"><input type="text" id="problem-search" placeholder="Filter problems..."></div>
  {problems_html}
</div>

<!-- Good/Bad Analysis -->
<div class="tab-panel" data-tab="good-bad">
  <div class="charts-row">
    <div class="chart-box">
      <h3 class="good">Top Improvements ({len(good_cases)} total)</h3>
      <p style="font-size:12px;color:var(--muted);margin-bottom:8px">Cases where skill reduced slop without breaking tests</p>
      <ul class="analysis-list">{good_list or "<li class='empty'>None</li>"}</ul>
    </div>
    <div class="chart-box">
      <h3 class="bad">Regressions ({len(bad_cases)} total)</h3>
      <p style="font-size:12px;color:var(--muted);margin-bottom:8px">Cases where skill caused test failures</p>
      <ul class="analysis-list">{bad_list or "<li class='empty'>None</li>"}</ul>
    </div>
  </div>
  <div class="chart-box" style="margin-top:16px">
    <h3>No Effect ({len(no_change_cases)} cases)</h3>
    <p style="font-size:12px;color:var(--muted)">Cases where the skill made no code changes at all — wasted compute.</p>
  </div>
</div>

<script>
// Tab switching
document.querySelectorAll('.tab').forEach(t => {{
  t.addEventListener('click', () => {{
    document.querySelectorAll('.tab').forEach(x => x.classList.toggle('active', x === t));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.dataset.tab === t.dataset.tab));
  }});
}});

// Problem search
document.getElementById('problem-search')?.addEventListener('input', function(e) {{
  const q = e.target.value.toLowerCase();
  document.querySelectorAll('.problem-group').forEach(g => {{
    g.style.display = g.querySelector('.problem-name').textContent.toLowerCase().includes(q) ? '' : 'none';
  }});
}});

// Browse search + filter
function filterBrowse() {{
  const q = (document.getElementById('browse-search')?.value || '').toLowerCase();
  const v = document.getElementById('browse-filter')?.value || 'all';
  document.querySelectorAll('.browse-card').forEach(c => {{
    const prob = (c.dataset.problem || '').toLowerCase();
    const verdict = c.dataset.verdict || '';
    const matchQ = !q || prob.includes(q);
    const matchV = v === 'all' || verdict === v;
    c.style.display = (matchQ && matchV) ? '' : 'none';
  }});
}}
document.getElementById('browse-search')?.addEventListener('input', filterBrowse);
document.getElementById('browse-filter')?.addEventListener('change', filterBrowse);

// Slop chart
(function() {{
  const data = {slop_chart_data};
  const el = document.getElementById('slop-chart');
  const colors = {json.dumps(SLOP_COLORS)};
  const labels = {json.dumps(SLOP_LABELS)};
  const maxVal = Math.max(...Object.values(data), 1);
  let html = '';
  const sorted = Object.entries(data).sort((a,b) => b[1]-a[1]);
  sorted.forEach(([k, v]) => {{
    const pct = (v / maxVal * 100).toFixed(1);
    const color = colors[k] || '#777';
    const label = labels[k] || k;
    html += '<div class="chart-bar">' +
      '<div class="bar-label">' + label + '</div>' +
      '<div class="bar-track"><div class="bar-fill" style="width:' + pct + '%;background:' + color + '"></div></div>' +
      '<div class="bar-val">' + v + '</div></div>';
  }});
  el.innerHTML = html;
}})();

// Metric chart
(function() {{
  const data = {metric_chart_data};
  const el = document.getElementById('metric-chart');
  const entries = Object.entries(data).sort((a,b) => a[1].avg - b[1].avg);
  const maxAbs = Math.max(...entries.map(e => Math.abs(e[1].avg)), 0.01);
  let html = '';
  entries.forEach(([k, v]) => {{
    const pct = (Math.abs(v.avg) / maxAbs * 50).toFixed(1);
    const color = v.avg < 0 ? 'var(--add-fg)' : v.avg > 0 ? 'var(--del-fg)' : 'var(--muted)';
    const dir = v.avg < 0 ? 'right' : 'left';
    const shortLabel = k.split('.').pop();
    html += '<div class="chart-bar">' +
      '<div class="bar-label">' + shortLabel + '</div>' +
      '<div class="bar-track" style="position:relative">' +
        '<div style="position:absolute;left:50%;top:0;bottom:0;width:1px;background:var(--border)"></div>' +
        '<div class="bar-fill" style="width:' + pct + '%;background:' + color + ';margin-' + (v.avg < 0 ? 'left:calc(50% - ' + pct + '%)' : 'left:50%') + '"></div>' +
      '</div>' +
      '<div class="bar-val" style="color:' + color + '">' + (v.avg >= 0 ? '+' : '') + v.avg.toFixed(2) + '</div></div>';
  }});
  el.innerHTML = html;
}})();
</script>
</body>
</html>'''


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    base = BASE_DIR
    if not base.exists():
        print(f"Error: {base} not found")
        sys.exit(1)

    print("Collecting data...")
    data = build_all_data(base)
    print(f"Summary: {data['summary']['total_cases']} cases, "
          f"{data['summary']['good']} improved, {data['summary']['bad']} regressed")

    print("Generating HTML...")
    html_content = build_html(data)

    OUTPUT_HTML.write_text(html_content)
    print(f"Written to {OUTPUT_HTML}")
    print(f"File size: {OUTPUT_HTML.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
