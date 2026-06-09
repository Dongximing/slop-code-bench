#!/usr/bin/env python3
"""Focused analysis on EXACTLY the 9 problems + run paths from
docs/slopcodebench-9prob-summary.md (read-only).

For every (problem, model, condition) it reads the precise run directory named
in that doc, and for each checkpoint extracts:
  - Base run  : before-skill eval (the "Baseline" column)
  - Skill run : before-skill eval (the "Before") and after_skill eval ("After")
  - real agent work (stdout tool_use count) and final API error category
so we can explain the doc's numbers with trajectory evidence.
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path("outputs")
GROUPS = ["Core", "Functionality", "Regression", "Error"]

# (problem, model) -> {"base": run_dir, "skill": run_dir}  exactly as in the doc
PATHS = {
    ("cfgpipe", "pangu"): {
        "base": "pangu/claude_code-2.0.51_baseline_20260603T1458",
        "skill": "pangu/claude_code-2.0.51_review_refactor_20260602T0203",
    },
    ("code_search", "pangu"): {
        "base": "pangu/claude_code-2.0.51_baseline_20260603T0121",
        "skill": "pangu/claude_code-2.0.51_review_refactor_20260602T0203",
    },
    ("database_migration", "pangu"): {
        "base": "pangu/claude_code-2.0.51_baseline_20260604T1405",
        "skill": "pangu/claude_code-2.0.51_review_refactor_20260603T2011",
    },
    ("datagate", "pangu"): {
        "base": "pangu/claude_code-2.0.51_baseline_20260603T2011",
        "skill": "pangu/claude_code-2.0.51_review_refactor_20260603T2011",
    },
    ("env_manager", "pangu"): {
        "base": "pangu/claude_code-2.0.51_baseline_20260603T2011",
        "skill": "pangu/claude_code-2.0.51_review_refactor_20260604T1409",
    },
    ("etl_pipeline", "pangu"): {
        "base": "pangu/claude_code-2.0.51_baseline_20260603T1458",
        "skill": "pangu/claude_code-2.0.51_review_refactor_20260602T0203",
    },
    ("eve_industry", "pangu"): {
        "base": "pangu/claude_code-2.0.51_baseline_20260603T1458",
        "skill": "pangu/claude_code-2.0.51_review_refactor_20260602T0203",
    },
    ("eve_jump_planner", "pangu"): {
        "base": "pangu/claude_code-2.0.51_baseline_20260603T1458",
        "skill": "pangu/claude_code-2.0.51_review_refactor_20260602T0203",
    },
    ("eve_route_planner", "pangu"): {
        "base": "pangu/claude_code-2.0.51_baseline_20260604T1405",
        "skill": "pangu/claude_code-2.0.51_review_refactor_20260604T1409",
    },
    ("cfgpipe", "glm"): {
        "base": "glm-5-kimi/claude_code-2.0.51_just-solve_none_20260601T2147",
        "skill": "glm-5-kimi/claude_code-2.0.51_just-solve_none_20260602T2029",
    },
    ("code_search", "glm"): {
        "base": "glm-5-kimi/claude_code-2.0.51_baseline_20260604T2240",
        "skill": "glm-5-kimi/claude_code-2.0.51_review_refactor_20260604T2242",
    },
    ("database_migration", "glm"): {
        "base": "glm-5-kimi/claude_code-2.0.51_baseline_20260604T2240",
        "skill": "glm-5-kimi/claude_code-2.0.51_review_refactor_20260604T2242",
    },
    ("datagate", "glm"): {
        "base": "glm-5-kimi/claude_code-2.0.51_baseline_20260604T2240",
        "skill": "glm-5-kimi/claude_code-2.0.51_review_refactor_20260605T1540",
    },
    ("env_manager", "glm"): {
        "base": "glm-5-kimi/claude_code-2.0.51_baseline_20260604T2240",
        "skill": "glm-5-kimi/claude_code-2.0.51_review_refactor_20260604T2242",
    },
    ("etl_pipeline", "glm"): {
        "base": "glm-5-kimi/claude_code-2.0.51_baseline_20260604T2240",
        "skill": "glm-5-kimi/claude_code-2.0.51_review_refactor_20260605T1540",
    },
    ("eve_industry", "glm"): {
        "base": "glm-5-kimi/claude_code-2.0.51_baseline_20260604T2240",
        "skill": "glm-5-kimi/claude_code-2.0.51_review_refactor_20260604T2242",
    },
    ("eve_jump_planner", "glm"): {
        "base": "glm-5-kimi/claude_code-2.0.51_baseline_20260604T2240",
        "skill": "glm-5-kimi/claude_code-2.0.51_review_refactor_20260605T1540",
    },
    ("eve_route_planner", "glm"): {
        "base": "glm-5-kimi/claude_code-2.0.51_baseline_20260604T2240",
        "skill": "glm-5-kimi/claude_code-2.0.51_review_refactor_20260605T1540",
    },
}

PROBLEMS = [
    "cfgpipe", "code_search", "database_migration", "datagate", "env_manager",
    "etl_pipeline", "eve_industry", "eve_jump_planner", "eve_route_planner",
]


def read_json(p: Path):
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def eval_counts(ev):
    if not ev:
        return None
    pc = ev.get("pass_counts", {})
    tc = ev.get("total_counts", {})
    return {g: (pc.get(g, 0), tc.get(g, 0)) for g in GROUPS}


def stdout_signals(stdout_path: Path):
    n_tool = 0
    result = None
    if not stdout_path.exists():
        return 0, None, None
    for line in stdout_path.open():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("type") == "result":
            result = d
        msg = d.get("message", {})
        if isinstance(msg, dict) and isinstance(msg.get("content"), list):
            for it in msg["content"]:
                if isinstance(it, dict) and it.get("type") == "tool_use":
                    n_tool += 1
    cat = None
    if result is not None:
        rtxt = str(result.get("result") or "")
        if "maximum context length" in rtxt or ("max_tokens" in rtxt and "too large" in rtxt):
            cat = "ctx400"
        elif "额度不足" in rtxt or "403" in rtxt:
            cat = "quota403"
        elif result.get("is_error") or "API Error" in rtxt:
            cat = "api_err"
        else:
            cat = "ok"
    return n_tool, cat, result.get("is_error") if result else None


def checkpoints_of(run_problem_dir: Path):
    cks = []
    if not run_problem_dir.is_dir():
        return cks
    for d in run_problem_dir.glob("checkpoint_*"):
        idx = d.name.split("_")[1]
        if idx.isdigit():
            cks.append((int(idx), d))
    return sorted(cks)


def collect():
    rows = []
    for problem in PROBLEMS:
        for model in ["pangu", "glm"]:
            paths = PATHS[(problem, model)]
            base_dir = OUT / paths["base"] / problem
            skill_dir = OUT / paths["skill"] / problem
            # union of checkpoint indices
            idxs = sorted(
                {i for i, _ in checkpoints_of(base_dir)}
                | {i for i, _ in checkpoints_of(skill_dir)}
            )
            for i in idxs:
                bdir = base_dir / f"checkpoint_{i}"
                sdir = skill_dir / f"checkpoint_{i}"
                base_ev = eval_counts(read_json(bdir / "evaluation.json"))
                skill_before = eval_counts(read_json(sdir / "evaluation.json"))
                skill_after = eval_counts(read_json(sdir / "after_skill" / "evaluation.json"))
                b_tools, b_cat, _ = stdout_signals(bdir / "agent" / "stdout.jsonl")
                s_tools, s_cat, _ = stdout_signals(sdir / "agent" / "stdout.jsonl")
                rows.append({
                    "problem": problem, "model": model, "ckpt": i,
                    "base_eval": base_ev, "base_tools": b_tools, "base_cat": b_cat,
                    "skill_before": skill_before, "skill_after": skill_after,
                    "skill_tools": s_tools, "skill_cat": s_cat,
                })
    return rows


if __name__ == "__main__":
    rows = collect()
    Path("analysis/nineprob.json").write_text(json.dumps(rows, indent=2))
    print(f"collected {len(rows)} checkpoint rows -> analysis/nineprob.json")
