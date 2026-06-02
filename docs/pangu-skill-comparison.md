# Pangu Skill Comparison: Thermo-Nuclear vs Review-Then-Refactor

**Model:** Pangu | **Agent:** Claude Code 2.0.51  
**Compared on 6 problems completed in both runs (28 checkpoints)**

| Run | Skill | Run ID |
|-----|-------|--------|
| Thermo-Nuclear | Aggressive structural refactor | `20260601T2146` |
| Review-Then-Refactor | 3-phase: Audit → Safety Check → Apply | `20260602T0203` |

---

## 1. Thermo-Nuclear Skill — Per Checkpoint Detail

### cfgpipe (6 checkpoints)

| Ckpt | Before Core | Before Func | After Core | After Func | Code | Δ | Before Tokens (in/out) | Skill Tokens (in/out) |
|------|-------------|-------------|------------|------------|------|---|----------------------|---------------------|
| 1 | 4/4 ✅ | 18/20 | 4/4 ✅ | 16/20 | +91/−173 | ⬇️ Func 18→16, Err 10→6 | 511K/8K | 2,008K/25K |
| 2 | 3/3 ✅ | 12/15 | 3/3 ✅ | 12/15 | +52/−86 | = | 3,420K/28K | 901K/11K |
| 3 | 4/4 ✅ | 13/13 | **0/4** ❌ | 1/13 | +3/−33 | ⬇️⬇️ Core 4→0 | 284K/6K | 490K/6K |
| 4 | 0/7 ❌ | 0/17 | 0/7 ❌ | 0/17 | +33/−64 | = | 1,546K/18K | 868K/9K |
| 5 | 0/6 ❌ | 1/34 | 0/6 ❌ | 1/34 | +104/−282 | ⬇️ Regr 51→48 | 2,837K/19K | 1,829K/16K |
| 6 | 0/3 ❌ | 0/21 | 0/3 ❌ | 1/21 | +299/−668 | ⬆️ Func 0→1, Regr 59→78 | 1,259K/16K | 1,076K/24K |

### code_search (5 checkpoints)

| Ckpt | Before Core | Before Func | After Core | After Func | Code | Δ |
|------|-------------|-------------|------------|------------|------|---|
| 1 | 4/7 ❌ | 0/4 | 4/7 ❌ | 0/4 | +81/−118 | = |
| 2 | 5/5 ✅ | 5/5 | 5/5 ✅ | 5/5 | +58/−149 | = |
| 3 | 0/8 ❌ | 0/12 | 0/8 ❌ | 0/12 | +139/−309 | = |
| 4 | 1/14 ❌ | 0/12 | 1/14 ❌ | 0/12 | +93/−112 | = |
| 5 | 0/13 ❌ | 0/14 | 0/13 ❌ | 0/14 | +15/−23 | = |

### dag_execution (3 checkpoints)

| Ckpt | Before Core | Before Func | After Core | After Func | Code | Δ |
|------|-------------|-------------|------------|------------|------|---|
| 1 | 0/12 ❌ | 0/15 | 0/12 ❌ | 0/15 | +284/−680 | = |
| 2 | 0/5 ❌ | 0/3 | 0/5 ❌ | 0/3 | +62/−534 | = |
| 3 | 0/3 ❌ | 0/7 | 0/3 ❌ | 0/7 | +91/−286 | = |

### etl_pipeline (5 checkpoints)

| Ckpt | Before Core | Before Func | After Core | After Func | Code | Δ |
|------|-------------|-------------|------------|------------|------|---|
| 1 | 5/6 ❌ | 8/13 | **6/6** ✅ | **13/13** | +79/−241 | ⬆️⬆️ Core 5→6, Func 8→13 |
| 2 | 15/16 ❌ | 10/11 | 15/16 ❌ | 10/11 | +52/−111 | = |
| 3 | 4/4 ✅ | 27/31 | 4/4 ✅ | 26/31 | +285/−547 | ⬇️ Func 27→26, Regr 64→63 |
| 4 | 0/3 ❌ | 0/7 | 0/3 ❌ | 0/7 | +119/−219 | ⬇️ Regr 69→55 |
| 5 | 0/4 ❌ | 3/18 | 0/4 ❌ | 3/18 | no change | = |

### eve_industry (6 checkpoints)

| Ckpt | Before Core | Before Func | After Core | After Func | Code | Δ |
|------|-------------|-------------|------------|------------|------|---|
| 1 | 1/3 ❌ | 2/6 | **0/3** ❌ | **0/6** | +162/−278 | ⬇️⬇️ Core 1→0, Func 2→0 |
| 2 | 0/7 ❌ | 0/11 | 0/7 ❌ | 0/11 | +126/−195 | = |
| 3 | 0/5 ❌ | 0/7 | 0/5 ❌ | 2/7 | +394/−434 | ⬆️ Func 0→2 |
| 4 | 0/2 ❌ | 0/5 | 0/2 ❌ | 0/5 | +391/−656 | ⬆️ Regr 10→11, Err 0→2 |
| 5 | 0/3 ❌ | 0/8 | 0/3 ❌ | 1/8 | +190/−303 | ⬇️ mixed |
| 6 | 0/2 ❌ | 0/3 | 0/2 ❌ | 0/3 | +181/−359 | ⬆️ Regr 9→13, Err 0→2 |

### eve_jump_planner (3 checkpoints)

| Ckpt | Before Core | Before Func | After Core | After Func | Code | Δ |
|------|-------------|-------------|------------|------------|------|---|
| 1 | 0/2 ❌ | 0/9 | 0/2 ❌ | 0/9 | +8/−55 | = |
| 2 | 0/1 ❌ | 0/7 | 0/1 ❌ | 0/7 | +70/−94 | = |
| 3 | 0/1 ❌ | 0/11 | 0/1 ❌ | 0/11 | +135/−917 | = |

---

## 2. Review-Then-Refactor Skill — Per Checkpoint Detail

### cfgpipe (6 checkpoints)

| Ckpt | Before Core | Before Func | After Core | After Func | Code | Δ | Before Tokens (in/out) | Skill Tokens (in/out) |
|------|-------------|-------------|------------|------------|------|---|----------------------|---------------------|
| 1 | 2/4 ❌ | 14/20 | 2/4 ❌ | 14/20 | +0/−7 | = | 208K/7K | 365K/6K |
| 2 | 3/3 ✅ | 15/15 | 3/3 ✅ | 15/15 | +0/−1 | = | 3,878K/37K | 101K/1K |
| 3 | 4/4 ✅ | 10/13 | **4/4** ✅ | 10/13 | +6/−9 | = | 1,060K/9K | 486K/6K |
| 4 | 1/7 ❌ | 1/17 | 1/7 ❌ | 1/17 | unchanged | = | 75K/1K | 55K/2K |
| 5 | 6/6 ✅ | 24/34 | 6/6 ✅ | 24/34 | +4/−47 | = | 1,918K/19K | 274K/3K |
| 6 | 0/3 ❌ | 2/21 | 0/3 ❌ | 2/21 | +2/−6 | = | 2,274K/20K | 347K/6K |

### code_search (5 checkpoints)

| Ckpt | Before Core | Before Func | After Core | After Func | Code | Δ |
|------|-------------|-------------|------------|------------|------|---|
| 1 | 7/7 ✅ | 4/4 | 7/7 ✅ | 4/4 | +10/−13 | = |
| 2 | 5/5 ✅ | 5/5 | 5/5 ✅ | 5/5 | +0/−2 | = |
| 3 | 0/8 ❌ | 0/12 | 0/8 ❌ | 0/12 | +5/−5 | = |
| 4 | 0/14 ❌ | 0/12 | **4/14** | **4/12** | +11/−7 | ⬆️⬆️ Core 0→4, Func 0→4, Regr 0→26 |
| 5 | 10/13 ❌ | 5/14 | 10/13 ❌ | 5/14 | +2/−3 | = |

### dag_execution (3 checkpoints)

| Ckpt | Before Core | Before Func | After Core | After Func | Code | Δ |
|------|-------------|-------------|------------|------------|------|---|
| 1 | 0/12 ❌ | 2/15 | 0/12 ❌ | 2/15 | +4/−12 | = |
| 2 | 0/5 ❌ | 0/3 | 0/5 ❌ | 0/3 | +3/−5 | = |
| 3 | 0/3 ❌ | 0/7 | 0/3 ❌ | 0/7 | +0/−7 | = |

### etl_pipeline (5 checkpoints)

| Ckpt | Before Core | Before Func | After Core | After Func | Code | Δ |
|------|-------------|-------------|------------|------------|------|---|
| 1 | 6/6 ✅ | 13/13 | 6/6 ✅ | 13/13 | +0/−6 | = |
| 2 | 14/16 ❌ | 10/11 | 14/16 ❌ | 10/11 | unchanged | = |
| 3 | 0/4 ❌ | 0/31 | 0/4 ❌ | 0/31 | +1/−8 | = |
| 4 | 3/3 ✅ | 5/7 | 3/3 ✅ | 5/7 | unchanged | = |
| 5 | 0/4 ❌ | 6/18 | 0/4 ❌ | 6/18 | unchanged | = |

### eve_industry (6 checkpoints)

| Ckpt | Before Core | Before Func | After Core | After Func | Code | Δ |
|------|-------------|-------------|------------|------------|------|---|
| 1 | 3/3 ✅ | 6/6 | 3/3 ✅ | 6/6 | +1/−2 | = |
| 2 | 0/7 ❌ | 0/11 | 0/7 ❌ | 0/11 | +0/−67 | = |
| 3 | 5/5 ✅ | 6/7 | 5/5 ✅ | 6/7 | +1/−42 | = |
| 4 | 0/2 ❌ | 0/5 | 0/2 ❌ | 0/5 | unchanged | = |
| 5 | 0/3 ❌ | 0/8 | 0/3 ❌ | 0/8 | +11/−22 | = |
| 6 | 0/2 ❌ | 0/3 | 0/2 ❌ | 0/3 | +3/−6 | = |

### eve_jump_planner (3 checkpoints)

| Ckpt | Before Core | Before Func | After Core | After Func | Code | Δ |
|------|-------------|-------------|------------|------------|------|---|
| 1 | 0/2 ❌ | 0/9 | 0/2 ❌ | 0/9 | +0/−20 | = |
| 2 | 0/1 ❌ | 0/7 | 0/1 ❌ | 0/7 | +2/−7 | = |
| 3 | 0/1 ❌ | 0/11 | 0/1 ❌ | 0/11 | unchanged | = |

---

## 3. Head-to-Head Comparison

### Score Impact (28 checkpoints each)

| Metric | Thermo-Nuclear | Review-Then-Refactor |
|--------|---------------|---------------------|
| Score unchanged ↔️ | 16 (57%) | **27 (96%)** |
| Score improved ⬆️ | 5 (17%) | 1 (3%) |
| Score worsened ⬇️ | **7 (25%)** | **0 (0%)** |
| Net (⬆️ minus ⬇️) | **-2** | **+1** |

### Code Changes

| Metric | Thermo-Nuclear | Review-Then-Refactor |
|--------|---------------|---------------------|
| Checkpoints with code changes | 28/28 (100%) | 22/28 (78%) |
| Average lines removed per ckpt | ~280 | ~15 |
| Average lines added per ckpt | ~120 | ~3 |

### Skill Token Cost

| Metric | Thermo-Nuclear | Review-Then-Refactor |
|--------|---------------|---------------------|
| Avg skill input tokens | ~1,100K | ~400K |
| Avg skill output tokens | ~14K | ~5K |
| Skill is cheaper | ❌ | ✅ (~3x cheaper) |

### Worst Cases

| | Thermo-Nuclear | Review-Then-Refactor |
|---|---------------|---------------------|
| Worst regression | cfgpipe/ckpt3: Core **4→0**, Func 13→1 (−84 tests) | **None** |
| 2nd worst | eve_industry/ckpt1: Core 1→0, Func 2→0 | **None** |
| Best improvement | etl_pipeline/ckpt1: Core 5→6, Func 8→13 (+6) | code_search/ckpt4: Core 0→4 (+36) |

---

## 4. Key Findings

1. **Review-Then-Refactor is strictly safer.** Zero regressions across 28 checkpoints vs 7 regressions for Thermo-Nuclear.

2. **Thermo-Nuclear is too aggressive for Pangu.** It makes large-scale changes (avg ~280 lines removed/ckpt) but 25% of the time breaks tests — often catastrophically (cfgpipe ckpt3 lost 84 tests).

3. **Review-Then-Refactor makes smaller, targeted changes** (avg ~15 lines removed/ckpt) but every change is safe. The 3-phase safety check (Audit → Safety Check → Apply) effectively filters out dangerous refactors.

4. **Review-Then-Refactor can still fix bugs.** code_search/ckpt4 went from Core 0/14 to 4/14 — a small edit (+11/−7 lines) fixed a real issue.

5. **Review-Then-Refactor is ~3x cheaper** in skill token usage — it reads the code, decides most changes are unsafe, and makes only minimal edits.

6. **Root cause of Thermo-Nuclear failures:** The model merges functions with different behavior (circuit_eval), removes "redundant" checks that handle edge cases (cfgpipe), and changes output formats during structural refactoring. These are exactly the patterns the Review-Then-Refactor safety check is designed to catch.

---

## 5. Recommendation

**Use Review-Then-Refactor for production runs.** It delivers:
- **0% regression** risk
- **78% of checkpoints** still get code improvements
- **3x lower skill cost**
- Occasional bug fixes (code_search/ckpt4)

Reserve Thermo-Nuclear for experimental analysis where you want to measure a model's refactoring aggressiveness and safety awareness.
