# GLM-5 cfgpipe: Baseline vs Skill Before vs Skill After

## Run Info

| Field | Value |
|-------|-------|
| **Problem** | cfgpipe (6 checkpoints) |
| **Model** | GLM-5 (`glm-5-kimi` via Kimi proxy at `http://1.95.77.23:3000`) |
| **Agent** | Claude Code 2.0.51 |
| **Skill** | Review-Then-Refactor (3-phase: Audit → Safety Check → Apply) |
| **Baseline run** | `outputs/glm-5-kimi/claude_code-2.0.51_just-solve_none_20260602T2028` |
| **Skill run** | `outputs/glm-5-kimi/claude_code-2.0.51_just-solve_none_20260602T2029` |
| **Config (baseline)** | `configs/runs/glm5_baseline.yaml` |
| **Config (skill)** | `configs/runs/glm5_review_then_refactor.yaml` |
| **Model config** | `configs/models/glm-5-kimi.yaml` (internal_name: `glm-5`, provider: `zhipu`) |
| **Agent config** | `configs/agents/claude_code_kimi.yaml` (base_url: `http://1.95.77.23:3000`) |

## Per-Checkpoint Results

### Checkpoint 1

| Metric | Baseline | Skill Before | Skill After | Baseline→After | Before→After |
|--------|----------|-------------|-------------|----------------|--------------|
| Core | 4/4 ✅ | 4/4 ✅ | 4/4 ✅ | 🟢 = | 🟢 = |
| Func | 20/20 | 20/20 | 20/20 | 🟢 = | 🟢 = |
| Error | 9/13 | 9/13 | 9/13 | 🟢 = | 🟢 = |
| Code changed | — | — | +16/−33 lines | | |

### Checkpoint 2

| Metric | Baseline | Skill Before | Skill After | Baseline→After | Before→After |
|--------|----------|-------------|-------------|----------------|--------------|
| Core | 3/3 ✅ | 3/3 ✅ | 3/3 ✅ | 🟢 = | 🟢 = |
| Func | 14/15 | 13/15 | 13/15 | 🔴 -1 | 🟢 = |
| Regr | 33/37 | 33/37 | 33/37 | 🟢 = | 🟢 = |
| Error | 11/13 | 8/13 | 8/13 | 🔴 -3 | 🟢 = |
| Code changed | — | — | +2/−4 lines | | |

### Checkpoint 3

| Metric | Baseline | Skill Before | Skill After | Baseline→After | Before→After |
|--------|----------|-------------|-------------|----------------|--------------|
| Core | 4/4 ✅ | 4/4 ✅ | 4/4 ✅ | 🟢 = | 🟢 = |
| Func | 10/13 | 11/13 | 11/13 | 🟢 +1 | 🟢 = |
| Regr | 61/68 | 60/68 | 60/68 | 🟢 +1 | 🟢 = |
| Error | 21/22 | 21/22 | 21/22 | 🟢 = | 🟢 = |
| Code changed | — | — | +3/−34 lines | | |

### Checkpoint 4

| Metric | Baseline | Skill Before | Skill After | Baseline→After | Before→After |
|--------|----------|-------------|-------------|----------------|--------------|
| Core | 6/7 ❌ | 6/7 ❌ | 6/7 ❌ | 🟢 = | 🟢 = |
| Func | 15/17 | 16/17 | 16/17 | 🟢 +1 | 🟢 = |
| Regr | 96/107 | 96/107 | 96/107 | 🟢 = | 🟢 = |
| Error | 4/6 | 4/6 | 4/6 | 🟢 = | 🟢 = |
| Code changed | — | — | +16/−199 lines | | |

### Checkpoint 5

| Metric | Baseline | Skill Before | Skill After | Baseline→After | Before→After |
|--------|----------|-------------|-------------|----------------|--------------|
| Core | 6/6 ✅ | 6/6 ✅ | 6/6 ✅ | 🟢 = | 🟢 = |
| Func | 28/34 | 29/34 | 29/34 | 🟢 +1 | 🟢 = |
| Regr | 121/137 | 122/137 | 122/137 | 🟢 +1 | 🟢 = |
| Error | 9/10 | 10/10 | 10/10 | 🟢 +1 | 🟢 = |
| Code changed | — | — | +6/−77 lines | | |

### Checkpoint 6

| Metric | Baseline | Skill Before | Skill After | Baseline→After | Before→After |
|--------|----------|-------------|-------------|----------------|--------------|
| Core | 3/3 ✅ | 3/3 ✅ | 3/3 ✅ | 🟢 = | 🟢 = |
| Func | 13/21 | 17/21 | 17/21 | 🟢 +4 | 🟢 = |
| Regr | 164/187 | 167/187 | 167/187 | 🟢 +3 | 🟢 = |
| Error | 4/5 | 5/5 | 5/5 | 🟢 +1 | 🟢 = |
| Code changed | — | — | +1/−86 lines | | |

## Aggregate

### Baseline → Skill After

| | 🟢 Improved | 🟢 Same | 🔴 Worsened |
|---|------------|---------|------------|
| Core | 0 | 6 | 0 |
| Func | 4 | 1 | 1 |
| Regr | 3 | 3 | 0 |
| Error | 2 | 3 | 1 |

Note: These differences are from **model non-determinism** (two independent runs), not the skill itself.

### Skill Before → Skill After (true skill effect)

| | 🟢 Improved | 🟢 Same | 🔴 Worsened |
|---|------------|---------|------------|
| Core | 0 | 6 | 0 |
| Func | 0 | 6 | 0 |
| Regr | 0 | 6 | 0 |
| Error | 0 | 6 | 0 |

**The skill modified code in all 6 checkpoints but caused zero score changes — strictly behavior-preserving.**

## Cost

| | Baseline | Skill Run (inference) |
|---|---------|----------------------|
| Total | $52.67 | $45.75 |
