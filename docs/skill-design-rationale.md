# Skill Design Rationale: Why Thermo-Nuclear Failed and How Review-Then-Refactor Fixes It

## Problem: Thermo-Nuclear Skill Breaks Tests

On 28 checkpoints (Pangu, 6 problems), the thermo-nuclear skill caused:
- **7 regressions (25%)** — tests that passed before now fail after applying the skill
- **5 improvements (17%)** — net negative outcome

### Root Cause 1: Merging Functions with Different Behavior

The thermo-nuclear prompt instructs: *"Delete whole layers of unnecessary indirection, wrappers, or pass-through helpers"* and *"Collapse duplicate branches into a single clearer flow."*

**What happened (circuit_eval/ckpt2, −18 tests):** The model saw `output_eval_error()` and `output_error()` — two functions that look similar but have different exit codes and JSON output formats. It merged them into one, breaking 18 tests.

```python
# BEFORE: Two separate functions with different behavior
def output_error(error, command, json_mode):     # exit code from error type
def output_eval_error(error, command, json_mode): # exit code from eval error mapping

# AFTER: Model merged into one — wrong exit codes for eval errors
def output_error(error, command, json_mode):      # lost the eval-specific mapping
```

### Root Cause 2: Removing "Redundant" Checks That Handle Edge Cases

The prompt instructs: *"Reframe state models so conditionals disappear"* and *"Remove ad-hoc conditionals."*

**What happened (cfgpipe/ckpt3, Core 4→0, −84 tests):** The model identified validation branches as "spaghetti" and removed them. These branches handled edge cases that the core tests rely on.

### Root Cause 3: Changing Constants and Output Formats

The prompt says nothing about preserving constants or output formats.

**What happened (file_merger/ckpt4):** The model changed `EXIT_AMBIGUOUS_FORMAT = 2` to `7` and added new exit code constants, breaking tests that check specific exit codes.

## Solution: Review-Then-Refactor Skill

The new skill addresses each root cause with a 3-phase approach:

### Phase 1: AUDIT
Read the code and produce a numbered list of findings, categorized by type (A-E). This forces the model to understand the code before touching it, rather than making changes as it reads.

### Phase 2: SAFETY CHECK
For each finding, the model must answer: *"Can this change produce different output for ANY input?"*

Explicit DROP rules prevent the three root causes:
- **"NEVER merge two functions that might have different behavior"** → prevents Root Cause 1
- **"NEVER remove a check/validation that might matter for edge cases"** → prevents Root Cause 2
- **"NEVER change error messages, exit codes, return values, or output format"** → prevents Root Cause 3
- **"If not 100% certain the change is safe, DROP it"** → catch-all safety net

### Phase 3: APPLY
Only APPROVED changes are applied, one at a time. The model re-reads the file after editing to verify correctness.

## Results (28 checkpoints, same 6 problems)

| Metric | Thermo-Nuclear | Review-Then-Refactor |
|--------|---------------|---------------------|
| Regressions (⬇️) | **7 (25%)** | **0 (0%)** |
| Improvements (⬆️) | 5 (17%) | 1 (3%) |
| Score unchanged | 57% | **96%** |
| Code changed | 100% of ckpts | 78% of ckpts |
| Avg lines changed/ckpt | ~400 | ~18 |
| Skill token cost | ~1,100K input | ~400K input (3x cheaper) |

## Design Principles

1. **Read before write.** Force the model to audit the full file before making any changes. This prevents premature refactoring based on incomplete understanding.

2. **Explicit safety gates.** Rather than relying on "do not change behavior" (which the model interprets liberally), provide a concrete checklist of prohibited changes.

3. **Conservative by default.** "A missed cleanup is fine. A broken test is not." This framing makes the model skip uncertain changes rather than attempting them.

4. **Small, targeted edits.** The safety check naturally filters out large structural changes (which are harder to verify as behavior-preserving), leaving only small, obviously-safe improvements.

5. **Categorized findings.** Categories A-D (dead code, comments, style, duplication) are almost always safe. Category E (structural) requires extra caution. This gives the model a framework for risk assessment.
