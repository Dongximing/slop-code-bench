# Pangu code_search: Code Health Comparison

**Tool:** [repo-analysis](../code-health/) (CodeScene-style health scoring)  
**Mode:** `--snapshot` (static analysis only, no Git history)  
**Problem:** code_search (5 checkpoints)

## Runs

| | Run | Description |
|---|-----|-------------|
| **Baseline** | `pangu/.../20260603T0121` | No skill applied |
| **Skill Before** | `pangu/.../20260602T0203` | Before review-then-refactor |
| **Skill After** | `pangu/.../20260602T0203/after_skill` | After review-then-refactor |

## Health Scores (code_search.py)

Health is scored 1-10 (10 = perfect). Penalties for nesting, complex conditionals, large functions.

| Ckpt | Baseline Health | Skill Before Health | Skill After Health | Before→After |
|------|----------------|--------------------|--------------------|--------------|
| 1 | 5.0 | 5.0 | 5.0 | 🟢 = |
| 2 | 5.0 | 5.0 | 5.0 | 🟢 = |
| 3 | 5.0 | 3.0 | 3.0 | 🟢 = |
| 4 | 3.0 | 3.0 | 3.0 | 🟢 = |
| 5 | 3.0 | 2.79 | 2.79 | 🟢 = |

## Structural Metrics (code_search.py)

| Ckpt | | Max Nesting | Complex Conditionals | Bool Operators | Functions |
|------|---|-------------|---------------------|----------------|-----------|
| 1 | Baseline | 5 | 4 | — | 7 |
| | Before | 4 | 4 | — | 8 |
| | After | 4 | 4 | — | 8 |
| 2 | Baseline | 5 | 5 | — | 7 |
| | Before | 4 | 4 | — | 9 |
| | After | 4 | 4 | — | 9 |
| 3 | Baseline | 5 | 7 | — | 21 |
| | Before | 10 | 92 | 126 | 17 |
| | After | 10 | 92 | 126 | 17 |
| 4 | Baseline | 9 | 17 | — | 26 |
| | Before | 10 | 99 | 126 | 27 |
| | After | 10 | 99 | 126 | 27 |
| 5 | Baseline | 9 | 17 | — | 26 |
| | Before | 10 | 181 | — | 31 |
| | After | 10 | 181 | — | 31 |

## Key Findings

### 1. Skill does not change code health scores
Before→After health is identical across all 5 checkpoints. The review-then-refactor skill makes safe, small edits (comments, dead code) that don't affect structural complexity metrics.

### 2. Health degrades across checkpoints
Both baseline and skill runs show health declining as checkpoints add complexity:
- Ckpt 1-2: health 5.0 (moderate)
- Ckpt 3-5: health drops to 3.0 → 2.79 (poor)

This is expected — later checkpoints add more features, increasing nesting and conditional complexity.

### 3. Baseline vs Skill Before show different code structures
Due to model non-determinism, baseline and skill-before produce structurally different code:
- **Baseline ckpt3**: nesting=5, complex_cond=7, 21 functions
- **Skill Before ckpt3**: nesting=10, complex_cond=92, 17 functions

The skill run's code is more complex (fewer but larger functions with deeper nesting), while baseline has more functions but shallower nesting. Neither approach is clearly better — they represent different decomposition strategies from the model.

### 4. Complex conditionals explode in later checkpoints
The skill run's code_search.py goes from 4 complex conditionals (ckpt1) to 181 (ckpt5). This suggests the model is adding feature logic through conditional branches rather than extracting abstractions — exactly the kind of "spaghetti growth" the thermo-nuclear skill was designed to catch, but review-then-refactor conservatively avoids restructuring.

## Interpretation

The review-then-refactor skill is **health-neutral**: it preserves code health by making only safe cosmetic changes. To actually improve code health (reduce nesting, simplify conditionals), a more aggressive skill would be needed — but as shown in the thermo-nuclear comparison, aggressive refactoring risks breaking tests.

This presents a tradeoff:
- **Conservative skill** (review-then-refactor): safe (0% regression) but health-neutral
- **Aggressive skill** (thermo-nuclear): can improve structure but 25% risk of breaking tests
