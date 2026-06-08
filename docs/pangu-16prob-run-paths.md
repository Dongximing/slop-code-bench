# Pangu 16 Completed Problems — Run Paths

These are the runs that contain the valid completed data for the 16 matched problems.

## Runs to KEEP

| Run Directory | Problems |
|--------------|----------|
| `claude_code-2.0.51_just-solve_none_20260601T1425` | log_query |
| `claude_code-2.0.51_just-solve_none_20260601T2146` | code_search, dag_execution, eve_industry |
| `claude_code-2.0.51_review_refactor_20260602T0203` | cfgpipe, code_search, dag_execution, etl_pipeline, eve_industry, eve_jump_planner |
| `claude_code-2.0.51_baseline_20260603T1458` | cfgpipe, etl_pipeline, eve_jump_planner |
| `claude_code-2.0.51_baseline_20260603T2011` | datagate |
| `claude_code-2.0.51_review_refactor_20260603T2011` | database_migration, datagate |
| `claude_code-2.0.51_baseline_20260604T1405` | database_migration, env_manager, eve_route_planner, file_query_tool, layered_config_synthesizer |
| `claude_code-2.0.51_review_refactor_20260604T1409` | env_manager, eve_route_planner |
| `claude_code-2.0.51_baseline_20260605T1442` | l2m, migrate_configs, pwd_manager |
| `claude_code-2.0.51_review_refactor_20260605T1442` | database_migration, file_query_tool, l2m, layered_config_synthesizer, log_query, migrate_configs, pwd_manager |

## Per-Problem Paths

```
cfgpipe:
  baseline: outputs/pangu/claude_code-2.0.51_baseline_20260603T1458/cfgpipe/
  review:   outputs/pangu/claude_code-2.0.51_review_refactor_20260602T0203/cfgpipe/

code_search:
  baseline: outputs/pangu/claude_code-2.0.51_just-solve_none_20260601T2146/code_search/
  review:   outputs/pangu/claude_code-2.0.51_review_refactor_20260602T0203/code_search/

dag_execution:
  baseline: outputs/pangu/claude_code-2.0.51_just-solve_none_20260601T2146/dag_execution/
  review:   outputs/pangu/claude_code-2.0.51_review_refactor_20260602T0203/dag_execution/

database_migration:
  baseline: outputs/pangu/claude_code-2.0.51_baseline_20260604T1405/database_migration/
  review:   outputs/pangu/claude_code-2.0.51_review_refactor_20260605T1442/database_migration/

datagate:
  baseline: outputs/pangu/claude_code-2.0.51_baseline_20260603T2011/datagate/
  review:   outputs/pangu/claude_code-2.0.51_review_refactor_20260603T2011/datagate/

env_manager:
  baseline: outputs/pangu/claude_code-2.0.51_baseline_20260604T1405/env_manager/
  review:   outputs/pangu/claude_code-2.0.51_review_refactor_20260604T1409/env_manager/

etl_pipeline:
  baseline: outputs/pangu/claude_code-2.0.51_baseline_20260603T1458/etl_pipeline/
  review:   outputs/pangu/claude_code-2.0.51_review_refactor_20260602T0203/etl_pipeline/

eve_industry:
  baseline: outputs/pangu/claude_code-2.0.51_just-solve_none_20260601T2146/eve_industry/
  review:   outputs/pangu/claude_code-2.0.51_review_refactor_20260602T0203/eve_industry/

eve_jump_planner:
  baseline: outputs/pangu/claude_code-2.0.51_baseline_20260603T1458/eve_jump_planner/
  review:   outputs/pangu/claude_code-2.0.51_review_refactor_20260602T0203/eve_jump_planner/

eve_route_planner:
  baseline: outputs/pangu/claude_code-2.0.51_baseline_20260604T1405/eve_route_planner/
  review:   outputs/pangu/claude_code-2.0.51_review_refactor_20260604T1409/eve_route_planner/

file_query_tool:
  baseline: outputs/pangu/claude_code-2.0.51_baseline_20260604T1405/file_query_tool/
  review:   outputs/pangu/claude_code-2.0.51_review_refactor_20260605T1442/file_query_tool/

l2m:
  baseline: outputs/pangu/claude_code-2.0.51_baseline_20260605T1442/l2m/
  review:   outputs/pangu/claude_code-2.0.51_review_refactor_20260605T1442/l2m/

layered_config_synthesizer:
  baseline: outputs/pangu/claude_code-2.0.51_baseline_20260604T1405/layered_config_synthesizer/
  review:   outputs/pangu/claude_code-2.0.51_review_refactor_20260605T1442/layered_config_synthesizer/

log_query:
  baseline: outputs/pangu/claude_code-2.0.51_just-solve_none_20260601T1425/log_query/
  review:   outputs/pangu/claude_code-2.0.51_review_refactor_20260605T1442/log_query/

migrate_configs:
  baseline: outputs/pangu/claude_code-2.0.51_baseline_20260605T1442/migrate_configs/
  review:   outputs/pangu/claude_code-2.0.51_review_refactor_20260605T1442/migrate_configs/

pwd_manager:
  baseline: outputs/pangu/claude_code-2.0.51_baseline_20260605T1442/pwd_manager/
  review:   outputs/pangu/claude_code-2.0.51_review_refactor_20260605T1442/pwd_manager/
```

## Runs that can be DELETED

All other pangu runs not listed above contain only failed/incomplete data:

```
claude_code-2.0.51_baseline_20260603T0121  (only code_search - already in 20260601T2146)
claude_code-2.0.51_baseline_20260603T1623
claude_code-2.0.51_baseline_20260604T2049
claude_code-2.0.51_baseline_20260604T2235
claude_code-2.0.51_just-solve_none_20260531T1813
claude_code-2.0.51_just-solve_none_20260601T0257
claude_code-2.0.51_review_refactor_20260604T2049
claude_code-2.0.51_review_refactor_20260604T2235
claude_code-2.0.51_review_refactor_20260604T2236
claude_code-2.0.51_review_refactor_20260606T1316
```

**WARNING**: Do NOT delete runs listed in "Runs to KEEP" — they contain the only valid data for those problems.
