# 9-Problem Focused Analysis

## ★ Key point: the skill does NOT improve core tests; gains only come from the agent re-solving between checkpoints

The doc treats `AFTER−BASE` as the "skill effect", but BASE and SKILL are two independent solve attempts. Decompose it into "pure skill" vs "solve variance":

| Model | BASE | BEFORE | AFTER | Doc "skill gain"=AFTER−BASE | **Pure skill**=AFTER−BEFORE | **Solve variance**=BEFORE−BASE |
|---|--|--|--|--|--|--|
| Pangu | 62 | 115 | 119 | +57 | **+4** | **+53 (93%)** |
| GLM | 163 | 194 | 194 | +31 | **0** | **+31 (100%)** |

---

## Q1 — Why Pangu's core is worse than GLM's

Cumulative core is about half of GLM's, and Pangu only has 3/45 checkpoints with tools≤2 (it is genuinely working), so this is a **real capability gap**, not "didn't run".

| Cumulative core | BASE | SKILL-Before |
|---|---|---|
| Pangu | 62 | 115 |
| GLM | 163 | 194 |

**Root cause: Pangu lacks the "run → read error → fix" self-testing loop.** Tool composition in the BASE run:

| Model | Valid ckpt | Tools | Avg/ckpt | Bash | Bash share | Used Bash |
|---|--|--|--|--|--|--|
| Pangu | 42 | 1790 | 42.6 | 722 | **40%** | 41/42 |
| GLM | 45 | 2187 | 48.6 | 1134 | **52%** | 42/45 |

**Failure-type counts among core-failing checkpoints** (BASE run, only checkpoints where core is not fully passing; script `analysis/q1_failtypes.py`):

| Failure type | Pangu | GLM | Example |
|---|--|--|---|
| Wrong entry filename (entry-contract violation) | **7** | 0 | datagate C1–C7: produced a `datagate/` directory instead of `datagate.py` |
| Crash (exit≠0 / exception / syntax error) | **14** | 9 | KeyError, SyntaxError, exit code 1 |
| Self-introduced NameError | **4** | 0 | code_search C3/C4 |
| Empty / invalid output | **2** | 2 | env_manager: `JSONDecodeError` |
| **Subtotal "program didn't run cleanly"** | **27 (71%)** | **11 (50%)** | sum of the four above |
| Wrong logic (runs but output mismatch) | 8 | 11 | exits normally, only the result is wrong |
| Infrastructure / API | 3 | 0 | 0 work, ctx400 |
| **Total core-failing** | **38** | **22** | |

→ **71% of Pangu's core failures are "program didn't run cleanly"** (GLM 50%), and GLM scores **zero** on wrong filename / NameError — confirming the missing self-test loop, delivering non-running programs straight to core tests.

---

## Q2 — Why "With Skill" is "sometimes" worse than Base

How to tell: look at `Before→After` within the same SKILL run. If Before is already low and After doesn't drop further → it's unrelated to the skill, it's solve variance.

### All 8 core-regression samples: Before already equals After

| Sample | BASE | Before | After | Pure skill |
|---|--|--|--|--|
| GLM datagate C3 | 5/5 | 0/5 | 0/5 | **0** |
| GLM etl_pipeline C3 | 4/4 | 0/4 | 0/4 | **0** |
| GLM database_migration C2 | 2/3 | 0/3 | 0/3 | **0** |
| GLM cfgpipe C4 | 7/7 | 6/7 | 6/7 | **0** |
| Pangu env_manager C4 | 3/4 | 0/4 | 0/4 | **0** |
| Pangu etl_pipeline C5 | 2/4 | 0/4 | 0/4 | **0** |
| Pangu env_manager C3 | 2/3 | 0/3 | 0/3 | **0** |
| Pangu cfgpipe C1 | 4/4 | 2/4 | 2/4 | **0** |

→ Pure skill effect is exactly 0 in all 8: the low score is because that SKILL run failed to solve it that time, unrelated to the skill.

| Sample | Regression | churn | Mechanism |
|---|---|--|---|
| Pangu env_manager C3 | Func −1, Regr −1 | 192 | **Large-rewrite drift** (the only case within the 9 problems) |

---

## Q3 — Why eve_jump_planner / eve_route_planner are incomplete

**Evaluation granularity is too coarse**: too few core tests + exact-match + all-or-nothing; a single mismatch means 0%.

| Problem | C1 core test count | Issue |
|---|--|---|
| eve_route_planner | **1** | single exact-match point |
| eve_jump_planner | **2** | almost no gradient |

---

## Q4 — Why the skill neither helps nor harms many instances (core and func/reg/error)

### Core conclusion: neutrality is inevitable, not coincidental

| Question | Reason | Evidence |
|---|---|---|
| **Why it can't "harm"** | 76/84 (90%) either didn't touch code (C) or only did small behavior-preserving cleanup (A/B, median 13–20 lines) | Compare Q2: cases that truly broke things had churn 100+ lines |
| **Why it can't "help"** | post-checkpoint skill is "behavior-preserving cosmetic cleanup" (remove comments, `range(len)`→`enumerate`, merge imports), orthogonal to correctness | The 8 D-class cases: code was already failing, cleanup can't fix logic, core stays 0 |

Cleanup ≠ bug fixing, so on the test dimension the skill's **ceiling is neutral**.

Neutral checkpoints = **84 / 88** (GLM 45/45; Pangu 39/43, plus 2 missing evals), grouped by cause into four classes:

| Cause class | Meaning | Count | Median churn | Example |
|---|---|--|--|--|
| **A. All passing** | before pass rate ≥80%, still fully passing after small edits | **38** | 20 lines | `glm cfgpipe C1` (89%, churn 49) |
| **B. Partially passing** | before pass 20–80%, cleanup preserves the distribution | **25** | 13 lines | `pangu cfgpipe C5` (72%) |
| **C. No edits (no-op)** | churn=0: code already clean or deliberately left alone | **13** | 0 lines | `pangu database_migration C3` (89%) |
| **D. Already failing** | before pass rate ≤20%, cleanup can't fix the logic | **8** | 46 lines | `eve_jump_planner C1/C2`, `eve_route_planner C2` |
| | | **84** | | |

---

## One-page conclusions

| # | Topic | Conclusion |
|---|---|---|
| 0 | **Data framing** | The doc's 62/119/163/194 aggregation counts run-to-run solve variance as "skill effect". The true skill effect (same-run Before→After) = **GLM +0, Pangu +4**; suggest rewriting #2/#3/#7 to "cleanup skill has essentially no effect on core". |
| 1 | **Q1** | Pangu core ≈ half of GLM's, a real capability gap; typical failures = wrong entry name, runtime crash, NameError, empty output, rooted in weaker self-testing/debugging than GLM. |
| 2 | **Q2** | All 8 core regressions are solve variance (pure skill effect=0); true skill breakage is rare but the safety is **not robust** (other paths: "deleting an import → 134 tests wiped out", light skill is worse). |
| 3 | **Q3** | The two eve problems have only 1–2 core tests + exact-match + all-or-nothing, extremely low discrimination (both models finished running; purely an evaluation-design issue, not the agent failing to work). |
| 4 | **Q4** | 84/88 neutral: can't "harm" (90% no change or small cleanup), can't "help" (cleanup is orthogonal to correctness). The skill's ceiling on the test dimension is neutral. |
