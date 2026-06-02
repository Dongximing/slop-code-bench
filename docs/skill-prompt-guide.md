# Deslop Skill Prompt Guide

## Problem

The current deslop prompt says:
```
Review all the code you just wrote in this workspace.
```

This causes models (especially weaker ones like GLM-5, Pangu) to:
1. Scan the entire `/workspace` including test data files (`files/`, `tests/`, etc.)
2. Spend time "cleaning" files they didn't write (pre-existing test assets)
3. Miss the actual solution code, or make no meaningful changes

## Root Cause

The workspace contains:
```
/workspace/
├── backup_scheduler.py     ← agent wrote this (solution)
├── requirements.txt        ← agent wrote this
├── files/                  ← pre-existing test data (NOT agent code)
│   ├── A/B/C/D.py          ← test asset, not solution code
│   ├── M.py                ← test asset, not solution code
│   └── ...
├── venv/                   ← virtual environment
└── schedule.yaml           ← test artifacts agent created during testing
```

Models can't distinguish solution code from test assets. They see `.py` files everywhere and try to "clean" them all.

## Fix: Improved Prompt

The prompt should explicitly tell the model which files to review. The solution file name is available from the problem config's `entry_file` field.

### Option 1: Explicit file targeting (recommended)

```yaml
post_checkpoint_skill:
  prompt: |
    Review ONLY the Python files you created in /workspace that implement the solution.
    Do NOT modify any files under /workspace/files/, /workspace/tests/, /workspace/venv/, or any test data directories.
    
    Focus only on the main solution file(s) - typically the .py file(s) in /workspace root.
    
    Remove AI-generated slop:
    - Extra comments that a human wouldn't add (e.g. "# Parse the input", "# Return the result")
    - Multi-line docstrings that repeat what the function name already says
    - Extra defensive checks or try/catch blocks that aren't needed
    - Inline imports in Python (move to top of file)
    - range(len(x)) - use enumerate
    - if cond: return True else: return False - simplify
    - Unnecessary else/elif after return
    - Dead code, unused imports, unused variables
    - Excessive blank lines between logic blocks
    - Any style inconsistent with the file
    
    Do NOT change any behavior. Do NOT break any existing functionality.
    Do NOT modify test data files, configuration files, or requirements.txt.
  run_on: all
  eval_after: true
```

### Option 2: Two-step approach (more reliable for weak models)

```yaml
post_checkpoint_skill:
  prompt: |
    Step 1: Run `ls /workspace/*.py` to find your solution files.
    Step 2: Review ONLY those files. Ignore everything in subdirectories.
    
    Remove AI-generated slop from the solution files:
    - Extra comments that a human wouldn't add
    - Multi-line docstrings that repeat what the function name already says  
    - Extra defensive checks or try/catch blocks that aren't needed
    - Inline imports in Python (move to top of file)
    - range(len(x)) - use enumerate
    - if cond: return True else: return False - simplify
    - Unnecessary else/elif after return
    - Dead code, unused imports, unused variables
    - Excessive blank lines
    
    Do NOT change any behavior. Do NOT touch files you didn't write.
  run_on: all
  eval_after: true
```

### Option 3: Programmatic (most reliable, requires code change)

Instead of relying on the model to find the right files, the skill prompt could be templated with the actual solution file path from the problem config. This would require a code change in `runner.py` to inject the entry file name:

```python
# In _run_post_checkpoint_skill():
entry_file = self.run_spec.problem.entry_file
skill_prompt = skill_cfg.prompt.replace("{entry_file}", entry_file)
```

Then the yaml:
```yaml
post_checkpoint_skill:
  prompt: |
    Review ONLY /workspace/{entry_file}.py and any other .py files in /workspace root.
    ...
```

## Applying the Fix

Edit your run config yaml (e.g. `configs/runs/glm5_deslop.yaml`):

```yaml
post_checkpoint_skill:
  prompt: |
    Review ONLY the Python files you created in /workspace that implement the solution.
    Do NOT modify any files under /workspace/files/, /workspace/tests/, /workspace/venv/, or any test data directories.
    Focus only on the main solution file(s) - typically the .py file(s) in /workspace root.
    
    Remove AI-generated slop:
    - Extra comments that a human wouldn't add
    - Multi-line docstrings that repeat what the function name already says
    - Extra defensive checks or try/catch blocks that aren't needed
    - Inline imports in Python (move to top of file)
    - range(len(x)) - use enumerate
    - if cond: return True else: return False - simplify
    - Unnecessary else/elif after return
    - Dead code, unused imports, unused variables
    - Excessive blank lines between logic blocks
    
    Do NOT change any behavior. Do NOT break any existing functionality.
    Do NOT modify test data files, configuration files, or requirements.txt.
  run_on: all
  eval_after: true
```

## Verification

After a run, check `infer.log` for the skill phase:
```bash
grep "04:3[7-9]\|04:4" <run_dir>/<problem>/infer.log | grep "Received payload" | head -10
```

The model should be reading/editing only solution files (e.g. `backup_scheduler.py`), not test data files (e.g. `files/M.py`).
