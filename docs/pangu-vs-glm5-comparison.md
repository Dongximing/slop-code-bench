# Pangu vs GLM-5 Comparison Report

**Problems:** file_backup, execution_server  
**Agent:** Claude Code 2.0.51 | **Deslop:** post_checkpoint_skill on all checkpoints

---

## file_backup

| Ckpt | | Pangu Core | Pangu Func | GLM-5 Core | GLM-5 Func | Winner |
|------|---|-----------|-----------|-----------|-----------|--------|
| 1 | Before | 0/1 ❌ | 5/27 | 1/1 ✅ | 23/27 | **GLM-5** |
| | After | 0/1 ❌ | 5/27 | 1/1 ✅ | 23/27 | |
| 2 | Before | 0/1 ❌ | 1/17 | 1/1 ✅ | 12/17 | **GLM-5** |
| | After | 0/1 ❌ | 1/17 | 1/1 ✅ | 12/17 | |
| 3 | Before | 0/1 ❌ | 3/17 | 1/1 ✅ | 11/17 | **GLM-5** |
| | After | 0/1 ❌ | 3/17 | 1/1 ✅ | 11/17 | |
| 4 | Before | 0/1 ❌ | 1/20 | 1/1 ✅ | 15/20 | **GLM-5** |
| | After | N/A | N/A | 1/1 ✅ | 15/20 | |

**GLM-5 dominates** — Core 4/4 all passed vs Pangu 0/4.

---

## execution_server

| Ckpt | | Pangu Core | Pangu Func | GLM-5 Core | GLM-5 Func | Winner |
|------|---|-----------|-----------|-----------|-----------|--------|
| 1 | Before | 7/7 ✅ | 35/38 | 7/7 ✅ | 37/38 | Tie (GLM-5 +2 func) |
| | After | 7/7 ✅ | 35/38 | 7/7 ✅ | 37/38 | |
| 2 | Before | 6/6 ✅ | 1/1 | 6/6 ✅ | 1/1 | Tie |
| | After | 6/6 ✅ | 1/1 | 6/6 ✅ | 1/1 | |
| 3 | Before | 13/16 ❌ | 17/26 | 2/16 ❌ | 3/26 | **Pangu** |
| | After | 13/16 ❌ | 17/26 | 2/16 ❌ | 3/26 | |
| 4 | Before | 12/16 ❌ | 23/32 | 1/16 ❌ | 0/32 | **Pangu** |
| | After | 12/16 ❌ | 23/32 | 1/16 ❌ | 0/32 | |
| 5 | Before | 8/9 ❌ | 17/19 | 9/9 ✅ | 19/19 | **GLM-5** |
| | After | 8/9 ❌ | 17/19 | 9/9 ✅ | 19/19 | |
| 6 | Before | 0/14 ❌ | 0/11 | 8/14 ❌ | 5/11 | **GLM-5** |
| | After | 0/14 ❌ | 0/11 | 8/14 ❌ | 5/11 | |

**Mixed results** — Pangu stronger on ckpt3-4, GLM-5 stronger on ckpt5-6.

---

## Head-to-Head Summary

| Metric | Pangu | GLM-5 |
|--------|-------|-------|
| **file_backup Core passed** | 0/4 | **4/4** |
| **execution_server Core passed** | 2/6 | **3/6** |
| **Total Core checkpoints passed** | 2/10 | **7/10** |
| file_backup cost | $17.04 | $31.59 |
| execution_server cost | ~$12* | $12.56 |

*Pangu execution_server cost estimated from separate run.

---

## Deslop Comparison

| | Pangu | GLM-5 |
|---|-------|-------|
| Checkpoints with code changes | 7/10 | 5/10 |
| Score changes after deslop | **0** | **0** |
| Effect | Cosmetic only | Cosmetic only |

Both models: deslop changed code but never affected test scores.

---

## Key Takeaways

1. **GLM-5 is significantly better on file_backup** (4/4 core vs 0/4) — Pangu couldn't get basic scheduling logic right
2. **Pangu handles mid-complexity checkpoints better on execution_server** (ckpt3: 13/16 vs 2/16) — better at extending existing code
3. **GLM-5 is more resilient in later checkpoints** (ckpt5-6) — Pangu collapsed on ckpt6 (0/14 core)
4. **GLM-5 wins overall**: 7/10 core checkpoints passed vs Pangu's 2/10
5. **Deslop is model-agnostic safe**: neither model's scores changed after deslop, regardless of whether code was modified
