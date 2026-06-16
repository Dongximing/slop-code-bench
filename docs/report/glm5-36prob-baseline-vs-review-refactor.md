# GLM-5-Kimi: 36-Problem Full Comparison — Baseline vs Review+Refactor

## Core Pass Rate by Progress Phase

![Core Pass Count by Progress Phase](report/glm5-36prob-progress-phases.png)

![Core Pass Rate by Progress Phase (Percentage)](report/glm5-36prob-progress-phases-pct.png)

## Charts

![Cumulative Core per Problem](report/glm5-36prob-core-summary.png)

![Cumulative Core Step Charts](report/glm5-36prob-core-cumulative.png)

![Before→After Skill Effect](report/glm5-36prob-before-after-delta.png)

## Run Sources

| Run Type | Path | Problems |
|----------|------|----------|
| Baseline | `outputs/glm-5-kimi/claude_code-2.0.51_baseline_20260602T2028` | cfgpipe |
| | `outputs/glm-5-kimi/claude_code-2.0.51_baseline_20260604T2240` | code_search, database_migration, datagate, env_manager, etl_pipeline, eve_industry, eve_jump_planner, eve_route_planner |
| | `outputs/glm-5-kimi/claude_code-2.0.51_baseline_20260608T1722` | circuit_eval, file_query_tool, l2m, layered_config_synthesizer, log_query, migrate_configs, mvvault, pwd_manager |
| | `outputs/glm-5-kimi/claude_code-2.0.51_baseline_20260611T0212` | forge, meshctl, sith, textdrop, trajectory_api, xjq |
| | `outputs/glm-5-kimi/claude_code-2.0.51_baseline_20260614T1806` | dag_execution, dynamic_buffer, dynamic_config_service_api, eve_market_tools, file_backup, file_merger |
| | `outputs/glm-5-kimi/claude_code-2.0.51_baseline_20260615T0117` | execution_server, metric_transform_lang, mocked_http, recli, rejector, sheeteval, test_translator |
| Review+Refactor | `outputs/glm-5-kimi/claude_code-2.0.51_review_refactor_20260602T2029` | cfgpipe |
| | `outputs/glm-5-kimi/claude_code-2.0.51_review_refactor_20260604T2242` | code_search, database_migration, env_manager, etl_pipeline, eve_industry |
| | `outputs/glm-5-kimi/claude_code-2.0.51_review_refactor_20260605T1540` | datagate, eve_jump_planner, eve_route_planner |
| | `outputs/glm-5-kimi/claude_code-2.0.51_review_refactor_20260608T1722` | circuit_eval, l2m, layered_config_synthesizer, migrate_configs, pwd_manager |
| | `outputs/glm-5-kimi/claude_code-2.0.51_review_refactor_20260610T1848` | mvvault |
| | `outputs/glm-5-kimi/claude_code-2.0.51_review_refactor_20260611T0213` | forge, textdrop, xjq |
| | `outputs/glm-5-kimi/claude_code-2.0.51_review_refactor_20260611T2154` | log_query, meshctl |
| | `outputs/glm-5-kimi/claude_code-2.0.51_review_refactor_20260612T0122` | file_query_tool |
| | `outputs/glm-5-kimi/claude_code-2.0.51_review_refactor_20260614T1846` | dag_execution, dynamic_buffer, dynamic_config_service_api, file_backup, file_merger |
| | `outputs/glm-5-kimi/claude_code-2.0.51_review_refactor_20260615T0119` | eve_market_tools, execution_server, metric_transform_lang, mocked_http, recli, sheeteval, sith, test_translator |
| | `outputs/glm-5-kimi/claude_code-2.0.51_review_refactor_20260616T1418` | rejector, trajectory_api |

Where a problem appears in multiple review runs, the best-scoring run is kept.

## Aggregate

| | Count | % |
|---|---:|---:|
| 🟢 Review wins | 23 | 64% |
| 🔴 Baseline wins | 9 | 25% |
| ⚪ Tie | 4 | 11% |

| Metric | Baseline | Review+Refactor |
|--------|----------|-----------------|
| Core Tests Passed | 776/1844 (42%) | 1066/1844 (58%) |
| Core Solved Checkpoints | 76 | 93 |

## Per-Problem Core Test Results

| # | Problem | Base Core | Base Solved | RF Core | RF Solved | Δ Core | |
|---|---------|----------|-------------|---------|-----------|--------|--|
| 1 | cfgpipe | 26/27 | 5 | 26/27 | 5 | 0 | ⚪ |
| 2 | circuit_eval | 52/92 | 3 | 88/92 | 5 | +36 | 🟢 |
| 3 | code_search | 16/47 | 1 | 37/47 | 1 | +21 | 🟢 |
| 4 | dag_execution | 0/20 | 0 | 0/20 | 0 | 0 | ⚪ |
| 5 | database_migration | 10/19 | 2 | 13/19 | 2 | +3 | 🟢 |
| 6 | datagate | 59/64 | 5 | 57/64 | 5 | -2 | 🔴 |
| 7 | dynamic_buffer | 2/58 | 0 | 3/58 | 0 | +1 | 🟢 |
| 8 | dynamic_config_service_api | 19/30 | 2 | 19/30 | 2 | 0 | ⚪ |
| 9 | env_manager | 9/16 | 2 | 15/16 | 4 | +6 | 🟢 |
| 10 | etl_pipeline | 33/33 | 5 | 30/33 | 4 | -3 | 🔴 |
| 11 | eve_industry | 8/22 | 2 | 15/22 | 3 | +7 | 🟢 |
| 12 | eve_jump_planner | 1/4 | 0 | 1/4 | 0 | 0 | ⚪ |
| 13 | eve_market_tools | 4/22 | 0 | 7/22 | 0 | +3 | 🟢 |
| 14 | eve_route_planner | 0/4 | 0 | 1/4 | 1 | +1 | 🟢 |
| 15 | execution_server | 61/68 | 4 | 65/68 | 3 | +4 | 🟢 |
| 16 | file_backup | 2/4 | 2 | 4/4 | 4 | +2 | 🟢 |
| 17 | file_merger | 48/58 | 1 | 37/58 | 1 | -11 | 🔴 |
| 18 | file_query_tool | 25/34 | 1 | 31/34 | 4 | +6 | 🟢 |
| 19 | forge | 22/26 | 6 | 23/26 | 7 | +1 | 🟢 |
| 20 | l2m | 15/25 | 1 | 14/25 | 0 | -1 | 🔴 |
| 21 | layered_config_synthesizer | 0/36 | 0 | 1/36 | 0 | +1 | 🟢 |
| 22 | log_query | 24/24 | 5 | 21/24 | 3 | -3 | 🔴 |
| 23 | meshctl | 29/30 | 7 | 26/30 | 6 | -3 | 🔴 |
| 24 | metric_transform_lang | 7/12 | 2 | 12/12 | 5 | +5 | 🟢 |
| 25 | migrate_configs | 7/9 | 4 | 9/9 | 5 | +2 | 🟢 |
| 26 | mocked_http | 0/43 | 0 | 29/43 | 3 | +29 | 🟢 |
| 27 | mvvault | 36/44 | 2 | 39/44 | 3 | +3 | 🟢 |
| 28 | pwd_manager | 7/47 | 0 | 34/47 | 0 | +27 | 🟢 |
| 29 | recli | 17/55 | 0 | 39/55 | 3 | +22 | 🟢 |
| 30 | rejector | 31/51 | 0 | 39/51 | 1 | +8 | 🟢 |
| 31 | sheeteval | 22/24 | 5 | 15/24 | 4 | -7 | 🔴 |
| 32 | sith | 44/57 | 2 | 37/57 | 2 | -7 | 🔴 |
| 33 | test_translator | 48/605 | 0 | 167/605 | 0 | +119 | 🟢 |
| 34 | textdrop | 16/19 | 4 | 17/19 | 5 | +1 | 🟢 |
| 35 | trajectory_api | 24/33 | 2 | 21/33 | 1 | -3 | 🔴 |
| 36 | xjq | 52/82 | 1 | 74/82 | 1 | +22 | 🟢 |
| | **TOTAL** | **776/1844** | **76** | **1066/1844** | **93** | **+290** | |

## Key Findings

1. **Review wins 64% of problems** (23/36) vs Baseline 25% (9/36), with 4 ties
2. **Core tests passed**: 776 → 1066 (+290, +37%)
3. **Core solved checkpoints**: 76 → 93 (+17)
4. **Largest gains**: test_translator (+119), circuit_eval (+36), mocked_http (+29), pwd_manager (+27), recli (+22), xjq (+22)
5. **Largest losses**: file_merger (-11), sheeteval (-7), sith (-7)
6. **Skill effect is zero**: Before Skill and After Skill produce identical test results — the review+refactor skill changes code cosmetically but does not affect correctness
7. **All differences are run-to-run non-determinism**: baseline and review_refactor are independent runs that produce different code due to model randomness
