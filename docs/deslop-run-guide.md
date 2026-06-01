# Deslop Run Guide

## Quick Start

Run a model with post-checkpoint deslop skill:

```bash
export ZHIPU_API_KEY=<your-key>
uv run slop-code run -c configs/runs/glm5_deslop.yaml
```

## How It Works

After each checkpoint's agent inference completes:

1. Agent produces code → **snapshot** (before deslop)
2. Deslop skill runs on the same workspace → **after_skill/snapshot** (after deslop)
3. Both snapshots are evaluated independently
4. Next checkpoint continues from the deslop'd code

Output structure:
```
outputs/<model>/<run_name>/<problem>/
├── checkpoint_1/
│   ├── snapshot/              # code before deslop
│   ├── evaluation.json        # eval of before
│   ├── after_skill/
│   │   ├── snapshot/          # code after deslop
│   │   └── evaluation.json    # eval of after
│   └── agent/                 # agent traces
├── checkpoint_2/
│   └── ...
```

## Run Config Reference

```yaml
agent: claude_code_kimi          # agent config name (configs/agents/)
environment: docker-python3.12-uv
prompt: just-solve               # prompt template (configs/prompts/)

model:
  provider: zhipu                # provider name (configs/providers.yaml)
  name: glm-5-kimi              # model config name (configs/models/)

post_checkpoint_skill:
  prompt: |                      # prompt sent to the agent after each checkpoint
    Review all the code you just wrote in this workspace.
    Remove AI-generated slop:
    - Extra comments that a human wouldn't add
    - Extra defensive checks or try/catch blocks that are abnormal
    - Inline imports in Python (move to top of file)
    - range(len(x)) - use enumerate
    - if cond: return True else: return False - simplify
    - Any style inconsistent with the file
    Do NOT change any behavior. Do NOT break any existing functionality.
  run_on: all                    # "all" | "last" | ["checkpoint_1", "checkpoint_3"]
  eval_after: true               # whether to run pytest eval on the deslop'd code

problems:
  - file_backup
```

## `run_on` Options

| Value | Behavior |
|-------|----------|
| `"all"` | Run skill after every checkpoint |
| `"last"` | Run skill only after the final checkpoint |
| `["checkpoint_1", "checkpoint_3"]` | Run skill only after the specified checkpoints |

## Re-evaluating Without Re-running Agent

If you already have a completed run and just want to re-eval:

```bash
uv run slop-code eval outputs/<model>/<run_name>/ -e configs/environments/docker-python3.12-uv.yaml
```

To eval a single snapshot manually:

```bash
uv run slop-code eval-snapshot \
  outputs/<model>/<run_name>/<problem>/checkpoint_1/snapshot \
  -p <problem_name> -c checkpoint_1 \
  -e configs/environments/docker-python3.12-uv.yaml \
  -o /tmp/eval-output --json
```

## Creating a New Run Config

1. Copy an existing config:
```bash
cp configs/runs/glm5_deslop.yaml configs/runs/my_run.yaml
```

2. Edit model/provider/problems as needed.

3. Adjust `run_on` to control which checkpoints get deslop'd:
```yaml
# Only deslop checkpoint 1 and 3
post_checkpoint_skill:
  prompt: "..."
  run_on: [checkpoint_1, checkpoint_3]
  eval_after: true
```

4. Run:
```bash
uv run slop-code run -c configs/runs/my_run.yaml
```

## Prerequisites

- Docker running with the agent image built (`slop-code:claude_code-2.0.51-python3.12`)
- Kill stale containers before a new run:
  ```bash
  docker ps -q --filter "ancestor=slop-code:claude_code-2.0.51-python3.12" | xargs docker rm -f 2>/dev/null
  ```
- API key exported for your provider

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `cost=0, steps=0` | Agent crash on startup | Check `agent/stderr.log` |
| `EACCES: permission denied` | Stale container without correct mounts | Kill containers, re-run |
| `infrastructure_failure: true` | Eval can't run pytest | Check `evaluation/stderr.txt` |
| `Exception ignored: PermissionError .git` | Cleanup warning (harmless) | Ignore, doesn't affect results |
| Deslop doesn't change code | Model chose not to edit | Check infer.log for skill execution timing |
