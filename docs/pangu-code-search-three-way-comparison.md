# Pangu code_search: Baseline vs Skill Before vs Skill After

## Run Info

| Field | Value |
|-------|-------|
| **Problem** | code_search (5 checkpoints) |
| **Model** | Pangu (`pangu` via `127.0.0.1:8088` / ModelArts) |
| **Agent** | Claude Code 2.0.51 |
| **Skill** | Review-Then-Refactor (3-phase: Audit → Safety Check → Apply) |
| **Baseline run** | `outputs/pangu/claude_code-2.0.51_just-solve_none_20260603T0121` |
| **Skill run** | `outputs/pangu/claude_code-2.0.51_just-solve_none_20260602T0203` |
| **Config (baseline)** | `configs/runs/pangu_new_baseline.yaml` |
| **Config (skill)** | `configs/runs/pangu_review_then_refactor.yaml` |

## Per-Checkpoint Results

### Checkpoint 1

| Metric | Baseline | Skill Before | Skill After | Baseline→After | Before→After |
|--------|----------|-------------|-------------|----------------|--------------|
| Core | 6/7 ❌ | 7/7 ✅ | 7/7 ✅ | 🟢 +1 | 🟢 = |
| Func | 3/4 | 4/4 | 4/4 | 🟢 +1 | 🟢 = |
| Error | 2/2 | 2/2 | 2/2 | 🟢 = | 🟢 = |
| Code changed | — | — | +10/−13 lines | | |
| Steps | 0 | 0 | 0 | | |
| Cost | $0.57 | $1.56 | $0.00 | | |

### Checkpoint 2

| Metric | Baseline | Skill Before | Skill After | Baseline→After | Before→After |
|--------|----------|-------------|-------------|----------------|--------------|
| Core | 5/5 ✅ | 5/5 ✅ | 5/5 ✅ | 🟢 = | 🟢 = |
| Func | 5/5 | 5/5 | 5/5 | 🟢 = | 🟢 = |
| Regr | 11/13 | 13/13 | 13/13 | 🟢 +2 | 🟢 = |
| Error | 2/2 | 2/2 | 2/2 | 🟢 = | 🟢 = |
| Code changed | — | — | +0/−2 lines | | |
| Steps | 0 | 0 | 0 | | |
| Cost | $1.61 | $2.43 | $0.00 | | |

### Checkpoint 3

| Metric | Baseline | Skill Before | Skill After | Baseline→After | Before→After |
|--------|----------|-------------|-------------|----------------|--------------|
| Core | 0/8 ❌ | 0/8 ❌ | 0/8 ❌ | 🟢 = | 🟢 = |
| Func | 0/12 | 0/12 | 0/12 | 🟢 = | 🟢 = |
| Regr | 0/25 | 25/25 | 25/25 | 🟢 +25 | 🟢 = |
| Error | 0/2 | 1/2 | 1/2 | 🟢 +1 | 🟢 = |
| Code changed | — | — | +5/−5 lines | | |
| Steps | 3 | 1 | 0 | | |
| Cost | $3.18 | $3.74 | $0.00 | | |

### Checkpoint 4

| Metric | Baseline | Skill Before | Skill After | Baseline→After | Before→After |
|--------|----------|-------------|-------------|----------------|--------------|
| Core | 0/14 ❌ | 0/14 ❌ | 4/14 ❌ | 🟢 +4 | 🟢 +4 |
| Func | 0/12 | 0/12 | 4/12 | 🟢 +4 | 🟢 +4 |
| Regr | 0/47 | 0/47 | 26/47 | 🟢 +26 | 🟢 +26 |
| Error | 1/2 | 1/2 | 2/2 | 🟢 +1 | 🟢 +1 |
| Code changed | — | — | +11/−7 lines | | |
| Steps | 0 | 2 | 1 | | |
| Cost | $2.09 | $4.00 | $0.00 | | |

### Checkpoint 5

| Metric | Baseline | Skill Before | Skill After | Baseline→After | Before→After |
|--------|----------|-------------|-------------|----------------|--------------|
| Core | 0/13 ❌ | 10/13 ❌ | 10/13 ❌ | 🟢 +10 | 🟢 = |
| Func | 0/14 | 5/14 | 5/14 | 🟢 +5 | 🟢 = |
| Regr | 4/75 | 36/75 | 36/75 | 🟢 +32 | 🟢 = |
| Error | 1/2 | 2/2 | 2/2 | 🟢 +1 | 🟢 = |
| Code changed | — | — | +2/−3 lines | | |
| Steps | 4 | 3 | 0 | | |
| Cost | $0.00 | $7.54 | $0.00 | | |

## Skill Trajectory Stats

| Ckpt | Phase | Steps | Tool Calls | Input Tokens | Output Tokens |
|------|-------|-------|------------|-------------|---------------|
| 1 | before | 0 | 0 | 478K | 8K |
| 1 | after | 0 | 0 | 306K | 8K |
| 2 | before | 0 | 0 | 777K | 7K |
| 2 | after | 0 | 0 | 93K | 3K |
| 3 | before | 1 | 1 | 1,132K | 23K |
| 3 | after | 0 | 0 | 470K | 6K |
| 4 | before | 2 | 2 | 1,262K | 14K |
| 4 | after | 1 | 1 | 819K | 3K |
| 5 | before | 3 | 3 | 2,434K | 16K |
| 5 | after | 0 | 0 | 721K | 6K |

## Aggregate

### Baseline → Skill After

| | 🟢 Improved | 🟢 Same | 🔴 Worsened |
|---|------------|---------|------------|
| Core | 2 (ckpt1 +1, ckpt4 +4) | 3 | 0 |
| Func | 2 (ckpt1 +1, ckpt4 +4) | 3 | 0 |
| Regr | 4 (ckpt2 +2, ckpt3 +25, ckpt4 +26, ckpt5 +32) | 1 | 0 |
| Error | 2 (ckpt3 +1, ckpt4 +1) | 3 | 0 |

Note: These differences are primarily from **model non-determinism** (two independent runs produce different code).

### Skill Before → Skill After (true skill effect)

| | 🟢 Improved | 🟢 Same | 🔴 Worsened |
|---|------------|---------|------------|
| Core | **1 (ckpt4 +4)** | 4 | 0 |
| Func | **1 (ckpt4 +4)** | 4 | 0 |
| Regr | **1 (ckpt4 +26)** | 4 | 0 |
| Error | **1 (ckpt4 +1)** | 4 | 0 |

**Checkpoint 4: The skill fixed a real bug.** A small edit (+11/−7 lines) improved Core from 0→4, Func from 0→4, and Regression from 0→26. Zero regressions across all checkpoints.

## Code Health Analysis (code_search.py)

Health scored 1-10 (10 = perfect) using [repo-analysis](../../code-health/) with CodeScene-style penalties for nesting, complex conditionals, and function size.

### Health Scores

| Ckpt | Baseline | Skill Before | Skill After | Baseline→After | Before→After |
|------|----------|-------------|-------------|----------------|--------------|
| 1 | 5.0 | 5.0 | 5.0 | ⚪ = | ⚪ = |
| 2 | 5.0 | 5.0 | 5.0 | ⚪ = | ⚪ = |
| 3 | 5.0 | 3.0 | 3.0 | 🔴 -2.0 | ⚪ = |
| 4 | 3.0 | 3.0 | 3.0 | ⚪ = | ⚪ = |
| 5 | 3.0 | 2.79 | 2.79 | 🔴 -0.21 | ⚪ = |

### Structural Metrics (code_search.py only)

| Ckpt | | Max Nesting | Complex Cond | Bool Ops | Functions |
|------|---|-------------|-------------|----------|-----------|
| 1 | Baseline | 5 | 4 | 6 | 7 |
| | Before | 4 | 4 | 4 | 8 |
| | After | 4 | 4 | 4 | 8 |
| 2 | Baseline | 5 | 5 | 7 | 7 |
| | Before | 4 | 4 | 4 | 9 |
| | After | 4 | 4 | 4 | 9 |
| 3 | Baseline | 5 | 7 | 9 | 21 |
| | Before | 10 | 92 | 118 | 17 |
| | After | 10 | 92 | 118 | 17 |
| 4 | Baseline | 9 | 17 | 22 | 26 |
| | Before | 10 | 99 | 126 | 27 |
| | After | 10 | 99 | 126 | 27 |
| 5 | Baseline | 9 | 17 | 22 | 26 |
| | Before | 10 | 181 | 238 | 31 |
| | After | 10 | 181 | 238 | 31 |

### Health vs Performance Tradeoff

| Ckpt | Better Health | Better Test Performance |
|------|--------------|----------------------|
| 1 | 🟢 Skill After (nesting -1) | 🟢 Skill After (Core 7/7 vs 6/7) |
| 2 | 🟢 Skill After (nesting -1, cc -1) | 🟢 Skill After (Regr 13/13 vs 11/13) |
| 3 | 🟢 **Baseline** (health 5.0 vs 3.0) | 🟢 **Skill After** (Regr 25/25 vs 0/25) |
| 4 | ⚪ Tie (health 3.0) | 🟢 **Skill After** (Core 4/14 vs 0/14) |
| 5 | 🟢 **Baseline** (health 3.0 vs 2.79) | 🟢 **Skill After** (Core 10/13 vs 0/13) |

**Ckpt3-5 show a "ugly but correct" vs "clean but broken" tradeoff.** The skill run's code has worse structural health (deeper nesting, 10x more complex conditionals) but passes far more tests. The baseline's code is structurally cleaner but functionally incorrect. This is due to model non-determinism, not the skill.

### Skill Effect on Health

**Before→After: zero change across all metrics.** The review-then-refactor skill modified code in all 5 checkpoints but only touched comments, dead code, and minor style — nothing that affects health scoring (nesting, conditionals, function size).

## Key Findings

1. **Review-Then-Refactor is safe**: 0 regressions across 5 checkpoints
2. **Skill can fix bugs**: ckpt4 gained 35 passing tests from an 18-line edit
3. **Baseline vs Skill Before shows large variance**: model non-determinism dominates (e.g. ckpt3 Regr 0/25 vs 25/25) — this is not skill effect
4. **Skill cost is minimal**: after-phase uses ~300K-800K input tokens, much less than inference phase

## Cost

| | Baseline | Skill Run (inference) |
|---|---------|----------------------|
| Total | $7.45 | $19.27 |

Note: Skill run is more expensive because inference phase was longer (different code paths due to non-determinism), not because of the skill itself.
