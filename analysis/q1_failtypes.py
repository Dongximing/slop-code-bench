#!/usr/bin/env python3
"""Count failure types delivered to CORE tests, for BOTH models (read-only).

For each (problem, model) BASE run, classify every checkpoint whose CORE tests
are not fully passing into one bucket, by parsing evaluation/stdout.txt +
stderr.txt. Mirrors the buckets used in analysis/q1_taxonomy.json so the Pangu
numbers reproduce, and extends the same logic to GLM for a side-by-side count.

    PYTHONPATH=analysis uv run python analysis/q1_failtypes.py
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nineprob import OUT, PATHS, PROBLEMS, checkpoints_of, read_json

# infra signals (tools / api error) precomputed in nineprob.json
NINE = {
    (r["problem"], r["model"], r["ckpt"]): r
    for r in json.loads(Path("analysis/nineprob.json").read_text())
}

EXC = [
    "ModuleNotFoundError", "ImportError", "KeyError", "TypeError",
    "AttributeError", "IndexError", "ValueError", "FileNotFoundError",
    "RuntimeError", "UnboundLocalError", "ZeroDivisionError",
]


def core_failing(ev) -> bool:
    if not ev:
        return True
    pc = ev.get("pass_counts", {}).get("Core", 0)
    tc = ev.get("total_counts", {}).get("Core", 0)
    return tc > 0 and pc < tc


def entry_present(ckpt_dir: Path, ev) -> bool:
    """Does the snapshot contain the expected entrypoint file?"""
    entry = (ev or {}).get("entrypoint", "")
    # entrypoint like "uv run datagate.py" -> datagate.py
    name = entry.split()[-1] if entry else ""
    snap = ckpt_dir / "snapshot"
    if not name.endswith(".py"):
        return True  # not a single-file contract; don't flag
    return (snap / name).exists()


def core_failed_tests(ev) -> list[str]:
    for k, v in (ev or {}).get("tests", {}).items():
        if k.endswith("-Core") and isinstance(v, dict):
            return list(v.get("failed", []))
    return []


def failed_reasons(ckpt_dir: Path) -> dict[str, str]:
    """test_name -> reason text, from pytest `FAILED ... - <reason>` lines."""
    out: dict[str, str] = {}
    p = ckpt_dir / "evaluation" / "stdout.txt"
    if not p.exists():
        return out
    for line in p.read_text(errors="replace").splitlines():
        if not (line.startswith("FAILED") or line.startswith("ERROR")):
            continue
        head, _, reason = line.partition(" - ")
        name = head.split("::")[-1].strip()
        out[name] = reason
    return out


CRASH_RE = re.compile(
    r"Command failed|returncode|Expected exit code|exit code 0, got"
    r"|non-zero exit|CalledProcessError|SyntaxError|IndentationError"
    r"|assert \d+ == 0")


def classify_reason(reason: str) -> str:
    if any(s in reason for s in ("JSONDecodeError", "Expecting value",
                                 "No JSON object", "Expecting property name")):
        return "empty_or_invalid_output"
    if CRASH_RE.search(reason) or any(e in reason for e in EXC):
        return "runtime_crash"
    if "AssertionError" in reason or reason.startswith("assert"):
        return "wrong_logic_output_mismatch"
    return "other_unclassified"


def classify(problem: str, model: str, ckpt: int, ckpt_dir: Path, ev) -> str:
    sig = NINE.get((problem, model, ckpt), {})
    tools = sig.get("base_tools") or 0
    cat_api = sig.get("base_cat")
    if tools == 0 or cat_api in ("ctx400", "quota403", "api_err"):
        return "infra_no_work_or_api"

    if not entry_present(ckpt_dir, ev):
        return "entry_file_contract"

    full = ckpt_dir / "evaluation" / "stdout.txt"
    txt = full.read_text(errors="replace") if full.exists() else ""

    # only the failing CORE tests' reasons (scopes out Error-group noise)
    reasons = failed_reasons(ckpt_dir)
    core_reasons = " || ".join(reasons[t] for t in core_failed_tests(ev)
                               if t in reasons)

    if "NameError" in txt:
        return "introduced_nameerror"
    if any(s in txt for s in ("JSONDecodeError", "Expecting value",
                              "No JSON object")) and "Command failed" not in core_reasons:
        return "empty_or_invalid_output"
    # crash = program raised / exited non-zero on core input
    if (any(e in txt for e in EXC) or "SyntaxError" in txt
            or "IndentationError" in txt or CRASH_RE.search(core_reasons)):
        return "runtime_crash"
    # remaining failures: program ran but produced wrong output
    return "wrong_logic_output_mismatch"


def run():
    per_model = {"pangu": Counter(), "glm": Counter()}
    rows = []
    for problem in PROBLEMS:
        for model in ("pangu", "glm"):
            base_dir = OUT / PATHS[(problem, model)]["base"] / problem
            for i, d in checkpoints_of(base_dir):
                ev = read_json(d / "evaluation.json")
                if not core_failing(ev):
                    continue
                cat = classify(problem, model, i, d, ev)
                per_model[model][cat] += 1
                rows.append((model, problem, i, cat))
    return per_model, rows


if __name__ == "__main__":
    per_model, rows = run()
    order = [
        "entry_file_contract", "runtime_crash", "introduced_nameerror",
        "empty_or_invalid_output", "wrong_logic_output_mismatch",
        "infra_no_work_or_api",
    ]
    print(f"{'category':32} {'pangu':>6} {'glm':>6}")
    for c in order:
        print(f"{c:32} {per_model['pangu'][c]:>6} {per_model['glm'][c]:>6}")
    print(f"{'TOTAL core-failing ckpts':32} "
          f"{sum(per_model['pangu'].values()):>6} "
          f"{sum(per_model['glm'].values()):>6}")
    Path("analysis/q1_failtypes.json").write_text(
        json.dumps([{"model": m, "problem": p, "ckpt": c, "cat": cat}
                    for m, p, c, cat in rows], indent=2))
