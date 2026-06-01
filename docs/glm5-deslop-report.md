# GLM-5 Deslop Report

**Run:** `outputs/glm-5-kimi/claude_code-2.0.51_just-solve_none_20260601T0247`  
**Model:** glm-5-kimi | **Agent:** Claude Code 2.0.51 | **Total Cost:** ~$114

---

## Before vs After Deslop — Per Checkpoint

### file_backup

| Ckpt | | Core | Func | Regr | Err | Code Changed |
|------|---|------|------|------|-----|-------------|
| 1 | Before | 1/1 ✅ | 23/27 | - | 2/4 | No |
| | After | 1/1 ✅ | 23/27 | - | 2/4 | |
| 2 | Before | 1/1 ✅ | 12/17 | 26/32 | - | No |
| | After | 1/1 ✅ | 12/17 | 26/32 | - | |
| 3 | Before | 1/1 ✅ | 11/17 | 39/50 | - | **Yes (-34 lines)** |
| | After | 1/1 ✅ | 11/17 | 39/50 | - | |
| 4 | Before | 1/1 ✅ | 15/20 | 51/68 | - | No |
| | After | 1/1 ✅ | 15/20 | 51/68 | - | |

### execution_server

| Ckpt | | Core | Func | Regr | Err | Code Changed |
|------|---|------|------|------|-----|-------------|
| 1 | Before | 7/7 ✅ | 37/38 | - | - | **Yes** |
| | After | 7/7 ✅ | 37/38 | - | - | |
| 2 | Before | 6/6 ✅ | 1/1 | 44/45 | 6/6 | **Yes** |
| | After | 6/6 ✅ | 1/1 | 44/45 | 6/6 | |
| 3 | Before | 2/16 ❌ | 3/26 | 57/58 | 1/1 | No |
| | After | 2/16 ❌ | 3/26 | 57/58 | 1/1 | |
| 4 | Before | 1/16 ❌ | 0/32 | 63/101 | - | No |
| | After | 1/16 ❌ | 0/32 | 63/101 | - | |
| 5 | Before | 9/9 ✅ | 19/19 | 96/157 | - | **Yes** |
| | After | 9/9 ✅ | 19/19 | 96/157 | - | |
| 6 | Before | 8/14 ❌ | 5/11 | 26/36 | 8/9 | **Yes** |
| | After | 8/14 ❌ | 5/11 | 26/36 | 8/9 | |

### file_merger

| Ckpt | | Core | Func | Regr | Err | Code Changed |
|------|---|------|------|------|-----|-------------|
| 1 | Before | 17/18 ❌ | 21/21 | - | 5/7 | **Yes** |
| | After | 17/18 ❌ | 21/21 | - | 5/7 | |
| 2 | Before | 8/11 ❌ | 15/23 | 40/46 | 6/6 | **Yes** |
| | After | 8/11 ❌ | 15/23 | 40/46 | 6/6 | |
| 3 | Before | 0/10 ❌ | 0/4 | 69/86 | 4/4 | **Yes** |
| | After | 0/10 ❌ | 0/4 | 69/86 | 4/4 | |
| 4 | Before | 0/19 ❌ | 5/11 | 71/104 | 9/13 | **Yes** |
| | After | 0/19 ❌ | 5/11 | 71/104 | 9/13 | |

### log_query

| Ckpt | | Core | Func | Regr | Err | Code Changed |
|------|---|------|------|------|-----|-------------|
| 1 | Before | 9/10 ❌ | 117/119 | - | 5/5 | **Yes** |
| | After | 9/10 ❌ | 117/119 | - | 5/5 | |
| 2 | Before | 5/5 ✅ | 59/64 | 131/134 | 4/4 | **Yes** |
| | After | 5/5 ✅ | 59/64 | 131/134 | 4/4 | |
| 3 | Before | 4/4 ✅ | 40/47 | 199/207 | 4/4 | **Yes** |
| | After | 4/4 ✅ | 40/47 | 199/207 | 4/4 | |
| 4 | Before | 2/2 ✅ | 28/31 | 247/262 | 4/4 | **Yes** |
| | After | 2/2 ✅ | 28/31 | 247/262 | 4/4 | |
| 5 | Before | 3/3 ✅ | 26/30 | 164/299 | 1/4 | N/A (run ended) |

---

## Deslop Impact Summary

| Metric | Value |
|--------|-------|
| Total checkpoints | 19 |
| Code changed by deslop | 13/19 (68%) |
| Score changed by deslop | **0/19 (0%)** |
| Total lines removed | -767 |
| Total lines added | +87 |
| Net lines removed | **-680** |

### What Deslop Changed

- Removed AI-generated comments and verbose docstrings
- Removed unused imports (`heapq`, `re`, `Callable`, etc.)
- Simplified `elif` after `return` to `if`
- Removed redundant `None` checks and empty lines
- Removed dead code branches

### Conclusion

Deslop modified code in 68% of checkpoints, removing ~680 lines of slop. **Test scores were identical in every single checkpoint** — deslop never broke anything, but also never fixed anything. The changes are purely cosmetic: cleaner code, same behavior.
