# Pangu: Baseline vs Skill After — 6 Problems

**Model:** Pangu | **Skill:** Review-Then-Refactor

![Performance vs Health](pangu-6prob-perf-vs-health.png)

## Summary

| Problem | Ckpts | Baseline Core Best | Skill After Core Best | Before→After |
|---------|-------|-------------------|----------------------|--------------|
| code_search | 5 | 5/5 (ckpt2) | 7/7 (ckpt1), 5/5 (ckpt2) | 🟢 1 improved, 4 same |
| cfgpipe | 6 | 4/4 (ckpt1) | 4/4 (ckpt3), 6/6 (ckpt5) | 🟢 6 same |
| etl_pipeline | 5 | 6/6 (ckpt1), 3/3 (ckpt4) | 6/6 (ckpt1), 3/3 (ckpt4) | 🟢 5 same |
| eve_industry | 6 | 5/5 (ckpt3) | 3/3 (ckpt1), 5/5 (ckpt3) | 🟢 6 same |
| eve_jump_planner | 3 | 0% all | 0% all | 🟢 3 same |
| datagate | 6 | 0% all | 5/5 (ckpt3), 6/6 (ckpt5) | 🟢 5 same, 🔴 1 (Err -2) |

### Skill Effect (Before → After): 31 checkpoints

| | 🟢 Improved | 🟢 Same | 🔴 Worsened |
|---|------------|---------|------------|
| Test scores | 1 | 29 | 1 |
| Code health | 0 | 31 | 0 |

### Baseline → Skill After: Performance

| Problem | Skill After wins | Baseline wins | Tie |
|---------|-----------------|--------------|-----|
| code_search | 4 | 0 | 1 |
| cfgpipe | 4 | 1 | 1 |
| etl_pipeline | 2 | 2 | 1 |
| eve_industry | 3 | 2 | 1 |
| eve_jump_planner | 0 | 0 | 3 |
| datagate | 5 | 0 | 1 |
| **Total** | **18** | **5** | **8** |

### Baseline → Skill After: Code Health

| Problem | Skill After better | Baseline better | Tie |
|---------|-------------------|----------------|-----|
| code_search | 2 | 2 | 1 |
| cfgpipe | 5 | 0 | 1 |
| etl_pipeline | 0 | 3 | 2 |
| eve_industry | 3 | 0 | 3 |
| eve_jump_planner | 0 | 2 | 1 |
| datagate | 6 | 0 | 0 |
| **Total** | **16** | **7** | **8** |
