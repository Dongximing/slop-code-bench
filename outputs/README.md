# Experiment Outputs

## Directory Structure

Each run follows this structure:

```
outputs/<model>/<run_name>/<problem>/checkpoint_N/
├── snapshot/                    ← BEFORE deslop (agent's original code)
│   └── *.py
├── after_skill/
│   └── snapshot/                ← AFTER deslop (cleaned code)
│       └── *.py
├── evaluation.json              ← Test results for BEFORE
├── after_skill/evaluation.json  ← Test results for AFTER
├── agent/
│   ├── stdout.jsonl             ← Agent trajectory (tool calls, model responses)
│   └── workspace/               ← Claude Code session transcripts
├── prompt.txt                   ← Prompt given to the agent
├── inference_result.json        ← Cost, tokens, timing
└── diff.json                    ← Code diff from previous checkpoint
```

**BEFORE deslop code:** `checkpoint_N/snapshot/*.py`  
**AFTER deslop code:** `checkpoint_N/after_skill/snapshot/*.py`

## Runs

### GLM-5 (via Kimi proxy)

| Run | Problems | Notes |
|-----|----------|-------|
| `glm-5-kimi/...20260530T0244` | file_backup | Early run, 4 checkpoints |
| `glm-5-kimi/...20260601T0247` | file_backup, execution_server, file_merger, log_query | Main run, 19 checkpoints |
| `glm-5-kimi/...20260601T1328` | env_manager | Separate run |

### Pangu

| Run | Problems | Notes |
|-----|----------|-------|
| `pangu/...20260531T1813` | file_backup | 4 checkpoints |
| `pangu/...20260601T0257` | execution_server | 6 checkpoints (file_merger interrupted) |
| `pangu/...20260601T1425` | file_merger, log_query, env_manager | Continuation run |

## Quick Access Examples

```bash
# GLM-5 file_backup checkpoint_1 — before deslop
cat outputs/glm-5-kimi/claude_code-2.0.51_just-solve_none_20260601T0247/file_backup/checkpoint_1/snapshot/backup_scheduler.py

# GLM-5 file_backup checkpoint_1 — after deslop
cat outputs/glm-5-kimi/claude_code-2.0.51_just-solve_none_20260601T0247/file_backup/checkpoint_1/after_skill/snapshot/backup_scheduler.py

# Compare before vs after
diff outputs/.../checkpoint_1/snapshot/backup_scheduler.py outputs/.../checkpoint_1/after_skill/snapshot/backup_scheduler.py

# View test results
cat outputs/.../checkpoint_1/evaluation.json           # before deslop
cat outputs/.../checkpoint_1/after_skill/evaluation.json  # after deslop

# View agent trajectory
cat outputs/.../checkpoint_1/agent/stdout.jsonl
```
