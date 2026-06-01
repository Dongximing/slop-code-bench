# Pangu Run Results

**Runs:**
- `outputs/pangu/claude_code-2.0.51_just-solve_none_20260531T1813` (file_backup)
- `outputs/pangu/claude_code-2.0.51_just-solve_none_20260601T0257` (execution_server)

**Model:** pangu | **Agent:** Claude Code 2.0.51 | **Deslop:** post_checkpoint_skill on all checkpoints

---

## Results Summary

| Problem | Checkpoints | Core Passed | Cost |
|---------|-------------|-------------|------|
| file_backup | 4 | 0/4 ❌ | $17.04 |
| execution_server | 6 | 2/6 (ckpt1,2) | ~$12 |

---

## Before vs After Deslop — Per Checkpoint

### file_backup

| Ckpt | | Core | Func | Regr | Err | Code Changed |
|------|---|------|------|------|-----|-------------|
| 1 | Before | 0/1 ❌ | 5/27 | - | 3/4 | Yes |
| | After | 0/1 ❌ | 5/27 | - | 3/4 | |
| 2 | Before | 0/1 ❌ | 1/17 | 8/32 | - | Yes |
| | After | 0/1 ❌ | 1/17 | 8/32 | - | |
| 3 | Before | 0/1 ❌ | 3/17 | 7/50 | - | No |
| | After | 0/1 ❌ | 3/17 | 7/50 | - | |
| 4 | Before | 0/1 ❌ | 1/20 | 10/68 | - | N/A |
| | After | N/A | | | | |

Core never passed. Regression worsened each checkpoint (8/32 → 7/50 → 10/68). Pangu struggled with basic scheduling logic (daily/weekly/once trigger times).

### execution_server

| Ckpt | | Core | Func | Regr | Err | Code Changed |
|------|---|------|------|------|-----|-------------|
| 1 | Before | 7/7 ✅ | 35/38 | - | - | Yes |
| | After | 7/7 ✅ | 35/38 | - | - | |
| 2 | Before | 6/6 ✅ | 1/1 | 42/45 | 6/6 | Yes |
| | After | 6/6 ✅ | 1/1 | 42/45 | 6/6 | |
| 3 | Before | 13/16 ❌ | 17/26 | 55/58 | 1/1 | Yes |
| | After | 13/16 ❌ | 17/26 | 55/58 | 1/1 | |
| 4 | Before | 12/16 ❌ | 23/32 | 83/101 | - | Yes |
| | After | 12/16 ❌ | 23/32 | 83/101 | - | |
| 5 | Before | 8/9 ❌ | 17/19 | 125/157 | - | Yes |
| | After | 8/9 ❌ | 17/19 | 125/157 | - | |
| 6 | Before | 0/14 ❌ | 0/11 | 17/36 | 1/9 | No |
| | After | 0/14 ❌ | 0/11 | 17/36 | 1/9 | |

Strong start (ckpt1-2 all core passed), competitive on ckpt3-4 (13/16, 12/16), then collapsed on ckpt6 (0/14).

---

## Deslop Impact Summary

| Metric | Value |
|--------|-------|
| Total checkpoints | 10 |
| Code changed by deslop | 7/10 (70%) |
| Score changed by deslop | **0/10 (0%)** |

### What Deslop Changed

- Removed dead code and unused variables (ckpt1 file_backup: deleted 5 lines of abandoned regex logic)
- Removed AI-generated comments
- Simplified expressions

### Conclusion

Deslop modified code in 70% of checkpoints but test scores were identical in every case. Changes were purely cosmetic — no behavioral impact.

---

## Key Observations

1. **file_backup is Pangu's weakness** — 0/4 core passed, basic scheduling/timezone logic broken throughout
2. **execution_server started strong** — ckpt1-2 perfect, ckpt3-4 competitive (13/16 and 12/16 core)
3. **Catastrophic collapse on ckpt6** — went from 8/9 core on ckpt5 to 0/14 on ckpt6
4. **Regression accumulation** — regression test failures grew each checkpoint (42/45 → 55/58 → 83/101 → 125/157)
5. **Deslop is safe** — never broke tests, removed minor slop only
