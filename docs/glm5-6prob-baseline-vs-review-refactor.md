# GLM-5-Kimi: 6-Problem Comparison — Baseline vs Before Skill vs After Skill

## Run Sources

| Run | Path |
|-----|------|
| Baseline | `claude_code-2.0.51_baseline_20260614T1806` |
| Before/After Skill | `claude_code-2.0.51_review_refactor_20260614T1846` |

## Aggregate (23 checkpoints, 6 problems)

| Comparison | Wins | Losses | Ties |
|------------|------|--------|------|
| Before Skill vs Baseline | 14 (60%) | 9 (39%) | 0 (0%) |
| After Skill vs Baseline | 14 (60%) | 9 (39%) | 0 (0%) |
| After Skill vs Before Skill | 0 (0%) | 0 (0%) | 23 (100%) |

## Cumulative Core Tests

| Metric | Baseline | Before Skill | After Skill |
|--------|----------|--------------|-------------|
| Core Tests Passed | 75/192 (39%) | 63/192 (32%) | 63/192 (32%) |
| Core Solved Checkpoints | 5/23 | 7/23 | 7/23 |
| Total Tests Passed | 698/1575 (44%) | 730/1575 (46%) | 730/1575 (46%) |

## Per-Problem Detail

### dag_execution (3 ckpts) — Core: Base 0/20, Before 0/20, After 0/20

| Ckpt | Baseline | Before Skill | After Skill | Δ vs Base |
|------|----------|--------------|-------------|-----------|
| 1 | C=0/12❌ (0/33) | C=0/12❌ (9/33) | C=0/12❌ (9/33) | 🟢 +9 |
| 2 | C=0/5❌ (0/41) | C=0/5❌ (9/41) | C=0/5❌ (9/41) | 🟢 +9 |
| 3 | C=0/3❌ (0/51) | C=0/3❌ (9/51) | C=0/3❌ (9/51) | 🟢 +9 |

### dynamic_buffer (4 ckpts) — Core: Base 2/58, Before 3/58, After 3/58

| Ckpt | Baseline | Before Skill | After Skill | Δ vs Base |
|------|----------|--------------|-------------|-----------|
| 1 | C=2/10❌ (10/30) | C=2/10❌ (9/30) | C=2/10❌ (9/30) | 🔴 -1 |
| 2 | C=0/10❌ (10/50) | C=1/10❌ (13/50) | C=1/10❌ (13/50) | 🟢 +3 |
| 3 | C=0/18❌ (10/104) | C=0/18❌ (13/104) | C=0/18❌ (13/104) | 🟢 +3 |
| 4 | C=0/20❌ (10/172) | C=0/20❌ (13/172) | C=0/20❌ (13/172) | 🟢 +3 |

### dynamic_config_service_api (4 ckpts) — Core: Base 19/30, Before 19/30, After 19/30

| Ckpt | Baseline | Before Skill | After Skill | Δ vs Base |
|------|----------|--------------|-------------|-----------|
| 1 | C=6/6✅ (35/47) | C=6/6✅ (40/47) | C=6/6✅ (40/47) | 🟢 +5 |
| 2 | C=6/6✅ (76/93) | C=6/6✅ (82/93) | C=6/6✅ (82/93) | 🟢 +6 |
| 3 | C=4/12❌ (32/76) | C=5/12❌ (24/76) | C=5/12❌ (24/76) | 🔴 -8 |
| 4 | C=3/6❌ (48/81) | C=2/6❌ (30/81) | C=2/6❌ (30/81) | 🔴 -18 |

### eve_market_tools (4 ckpts) — Core: Base 4/22, Before 0/22, After 0/22

| Ckpt | Baseline | Before Skill | After Skill | Δ vs Base |
|------|----------|--------------|-------------|-----------|
| 1 | C=0/2❌ (1/10) | C=0/2❌ (0/10) | C=0/2❌ (0/10) | 🔴 -1 |
| 2 | C=2/10❌ (5/28) | C=0/10❌ (0/28) | C=0/10❌ (0/28) | 🔴 -5 |
| 3 | C=0/2❌ (5/62) | C=0/2❌ (0/62) | C=0/2❌ (0/62) | 🔴 -5 |
| 4 | C=2/8❌ (9/75) | C=0/8❌ (0/75) | C=0/8❌ (0/75) | 🔴 -9 |

### file_backup (4 ckpts) — Core: Base 2/4, Before 4/4, After 4/4

| Ckpt | Baseline | Before Skill | After Skill | Δ vs Base |
|------|----------|--------------|-------------|-----------|
| 1 | C=1/1✅ (25/32) | C=1/1✅ (26/32) | C=1/1✅ (26/32) | 🟢 +1 |
| 2 | C=0/1❌ (27/50) | C=1/1✅ (39/50) | C=1/1✅ (39/50) | 🟢 +12 |
| 3 | C=1/1✅ (37/68) | C=1/1✅ (50/68) | C=1/1✅ (50/68) | 🟢 +13 |
| 4 | C=0/1❌ (42/89) | C=1/1✅ (66/89) | C=1/1✅ (66/89) | 🟢 +24 |

### file_merger (4 ckpts) — Core: Base 48/58, Before 37/58, After 37/58

| Ckpt | Baseline | Before Skill | After Skill | Δ vs Base |
|------|----------|--------------|-------------|-----------|
| 1 | C=17/18❌ (43/46) | C=8/18❌ (26/46) | C=8/18❌ (26/46) | 🔴 -17 |
| 2 | C=8/11❌ (70/86) | C=8/11❌ (73/86) | C=8/11❌ (73/86) | 🟢 +3 |
| 3 | C=10/10✅ (85/104) | C=10/10✅ (88/104) | C=10/10✅ (88/104) | 🟢 +3 |
| 4 | C=13/19❌ (118/147) | C=11/19❌ (111/147) | C=11/19❌ (111/147) | 🔴 -7 |

## Per-Problem Summary

| Problem | Ckpts | After>Base | Base>After | Tie |
|---------|-------|-----------|-----------|-----|
| dag_execution | 3 | 3 | 0 | 0 |
| dynamic_buffer | 4 | 3 | 1 | 0 |
| dynamic_config_service_api | 4 | 2 | 2 | 0 |
| eve_market_tools | 4 | 0 | 4 | 0 |
| file_backup | 4 | 4 | 0 | 0 |
| file_merger | 4 | 2 | 2 | 0 |
| **Total** | **23** | **14** | **9** | **0** |

## Cost & Efficiency

| Metric | Baseline | Skill After |
|--------|----------|-------------|
| Total Cost ($) | 175.26 | 212.98 |
| Mean Checkpoint Cost ($) | 7.62 | 9.26 |
| Mean Checkpoint Time (s) | 1024 | 1148 |
| Mean Steps/Checkpoint | 39.0 | 45.2 |
| Lint per LOC | 0.1746 | 0.1432 |

## Key Findings

1. **Skill effect is zero on GLM-5-Kimi**: Before Skill and After Skill produce identical test results across all 23 checkpoints — the review+refactor skill changed code cosmetically but broke/fixed nothing
2. **All differences are run-to-run non-determinism**: Before Skill vs Baseline shows 14 wins / 9 losses, entirely from the model generating different code in a separate run
3. **Core Solved**: Baseline 5/23 → Skill run 7/23 (Before = After) — net +2 from non-determinism alone
4. **file_backup benefits most from re-run**: +2 core checkpoints solved, +50 total tests gained
5. **eve_market_tools hurt most by re-run**: all tests 0 in skill run vs some passing in baseline
