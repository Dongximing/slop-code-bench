#!/usr/bin/env python3
"""
Dynamic buffer code generator - Part 2: Stateful Transforms.

Given a module name, a sample directory with input/output files,
a target language, and an output directory, this script:
1. Infers a deterministic transformation from input to output.
2. Supports stateful transforms: prefix, sliding window, state machine, neighbor-based filtering.
3. Generates a module implementing a DynamicPreprocessor that applies
   the inferred transformation with streaming support and resume safety.
"""

import argparse
import csv
import json
import os
import sys
import math
import hashlib
import re
from typing import Any, Optional
from collections import deque

# ── Data Parsing ────────────────────────────────────────────────────────

def detect_extension(sample_dir: str) -> str:
    """Detect the shared extension of input/output files in the sample dir."""
    for ext in ("csv", "tsv", "jsonl", "json"):
        inp = os.path.join(sample_dir, f"input.{ext}")
        out = os.path.join(sample_dir, f"output.{ext}")
        if os.path.isfile(inp) and os.path.isfile(out):
            return ext
    raise FileNotFoundError(
        f"No matching input.*/output.* pair found in {sample_dir}"
    )


def load_rows(path: str, ext: str) -> list[dict[str, Any]]:
    """Load rows from a data file with the given extension."""
    with open(path, "r", encoding="utf-8") as f:
        if ext == "csv":
            reader = csv.DictReader(f)
            return [dict(row) for row in reader]
        elif ext == "tsv":
            reader = csv.DictReader(f, delimiter="\t")
            return [dict(row) for row in reader]
        elif ext == "jsonl":
            return [json.loads(line) for line in f if line.strip()]
        elif ext == "json":
            return json.load(f)
    raise ValueError(f"Unsupported extension: {ext}")


# ── Value Normalisation ────────────────────────────────────────────────

def parse_value(v: Any) -> Any:
    """Normalise a raw parsed value for comparison."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v
    s = str(v)
    try:
        return int(s)
    except (ValueError, TypeError):
        pass
    try:
        return float(s)
    except (ValueError, TypeError):
        pass
    if s.lower() == "true":
        return True
    if s.lower() == "false":
        return False
    if s.lower() == "null" or s == "":
        return None
    return s


def values_equal(a: Any, b: Any) -> bool:
    """Compare two parsed values for equality."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if isinstance(a, bool) and isinstance(b, bool):
        return a == b
    if isinstance(a, bool) or isinstance(b, bool):
        return False
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) == float(b)
    return str(a) == str(b)


def try_numeric(v: Any):
    """Try to interpret value as numeric. Return (number, True) or (v, False)."""
    if isinstance(v, bool):
        return (v, False)
    if isinstance(v, (int, float)):
        return (float(v), True)
    if isinstance(v, str):
        try:
            return (float(v), True)
        except (ValueError, TypeError):
            return (v, False)
    return (v, False)


# ── Transformation Inference: Per-Row Filtering ─────────────────────────

def infer_filter_condition(
    input_rows: list[dict], output_rows: list[dict]
) -> str | None:
    """
    Infer a filtering condition that explains which input rows survive
    into the output. Returns a Python expression string (or None if no filtering).
    """
    if len(input_rows) == len(output_rows):
        return None

    kept_indices: list[int] = _match_rows(input_rows, output_rows)

    if len(kept_indices) == len(input_rows):
        return None  # all kept, no filter

    kept = [input_rows[i] for i in kept_indices]
    dropped = [input_rows[i] for i in range(len(input_rows)) if i not in set(kept_indices)]

    if not dropped:
        return None

    return _infer_predicate(kept, dropped, input_rows[0].keys())


def _match_rows(
    input_rows: list[dict], output_rows: list[dict]
) -> list[int]:
    """Match output rows to input rows greedily (order-preserving)."""
    kept = []
    out_idx = 0
    for in_idx, in_row in enumerate(input_rows):
        if out_idx >= len(output_rows):
            break
        if _rows_match(in_row, output_rows[out_idx]):
            kept.append(in_idx)
            out_idx += 1
    return kept


def _rows_match(in_row: dict, out_row: dict) -> bool:
    """Check if an input row could have produced an output row
    (considering only columns present in both)."""
    for key, val in out_row.items():
        if key in in_row:
            if not values_equal(parse_value(in_row[key]), parse_value(val)):
                return False
    return True


def _infer_predicate(
    kept: list[dict], dropped: list[dict], all_columns: list[str]
) -> str | None:
    """Infer a boolean predicate that is True for kept rows, False for dropped."""
    kept_n = [{k: parse_value(v) for k, v in r.items()} for r in kept]
    dropped_n = [{k: parse_value(v) for k, v in r.items()} for r in dropped]

    conditions: list[tuple[str, str]] = []

    for col in all_columns:
        kept_vals = [r.get(col) for r in kept_n]
        dropped_vals = [r.get(col) for r in dropped_n]

        unique_kept = set()
        for v in kept_vals:
            if v is None:
                unique_kept.add(None)
            elif isinstance(v, bool):
                unique_kept.add(v)
            elif isinstance(v, (int, float)):
                unique_kept.add(v)
            else:
                unique_kept.add(str(v))

        unique_dropped = set()
        for v in dropped_vals:
            if v is None:
                unique_dropped.add(None)
            elif isinstance(v, bool):
                unique_dropped.add(v)
            elif isinstance(v, (int, float)):
                unique_dropped.add(v)
            else:
                unique_dropped.add(str(v))

        for val in unique_kept:
            if val not in unique_dropped and len(unique_kept) == 1:
                cond = _make_eq_expr(col, val)
                if _check_condition(cond, kept_n, dropped_n, all_columns):
                    conditions.append((col, cond))

        for val in unique_dropped:
            if val not in unique_kept and len(unique_dropped) == 1:
                cond = _make_neq_expr(col, val)
                if _check_condition(cond, kept_n, dropped_n, all_columns):
                    conditions.append((col, cond))

        kept_nums = [try_numeric(v) for v in kept_vals]
        dropped_nums = [try_numeric(v) for v in dropped_vals]

        all_kept_numeric = all(is_num for _, is_num in kept_nums)
        all_dropped_numeric = all(is_num for _, is_num in dropped_nums)

        if all_kept_numeric and all_dropped_numeric:
            kn = [n for n, _ in kept_nums]
            dn = [n for n, _ in dropped_nums]
            if kn and dn:
                min_k, max_k = min(kn), max(kn)
                min_d, max_d = min(dn), max(dn)
                if min_d < min_k:
                    threshold = min_k
                    if all(d < threshold for d in dn) and all(k >= threshold for k in kn):
                        cond = _make_gte_expr(col, threshold)
                        if _check_condition(cond, kept_n, dropped_n, all_columns):
                            conditions.append((col, cond))
                if min_d <= min_k:
                    threshold = min_d
                    if all(d <= threshold for d in dn) and all(k > threshold for k in kn):
                        cond = _make_gt_expr(col, threshold)
                        if _check_condition(cond, kept_n, dropped_n, all_columns):
                            conditions.append((col, cond))
                if max_d > max_k:
                    threshold = max_k
                    if all(d > threshold for d in dn) and all(k <= threshold for k in kn):
                        cond = _make_lte_expr(col, threshold)
                        if _check_condition(cond, kept_n, dropped_n, all_columns):
                            conditions.append((col, cond))
                if max_d >= max_k:
                    threshold = max_d
                    if all(d >= threshold for d in dn) and all(k < threshold for k in kn):
                        cond = _make_lt_expr(col, threshold)
                        if _check_condition(cond, kept_n, dropped_n, all_columns):
                            conditions.append((col, cond))

    if conditions:
        return conditions[0][1]

    simple_conds = _get_all_simple_conditions(kept_n, dropped_n, all_columns)
    for c1 in simple_conds:
        for c2 in simple_conds:
            if c1 >= c2:
                continue
            combined = f"({c1} and {c2})"
            if _check_condition(combined, kept_n, dropped_n, all_columns):
                return combined

    for c1 in simple_conds:
        for c2 in simple_conds:
            if c1 >= c2:
                continue
            combined = f"({c1} or {c2})"
            if _check_condition(combined, kept_n, dropped_n, all_columns):
                return combined

    return None


def _get_all_simple_conditions(
    kept_n: list[dict], dropped_n: list[dict], all_columns: list[str]
) -> list[str]:
    """Get all simple conditions that might form part of a predicate."""
    conds = []
    for col in all_columns:
        kept_vals = [r.get(col) for r in kept_n]
        dropped_vals = [r.get(col) for r in dropped_n]

        unique_kept = set(_hashable(v) for v in kept_vals)
        unique_dropped = set(_hashable(v) for v in dropped_vals)

        for val_h in unique_kept:
            if val_h not in unique_dropped:
                val = _unhashable(val_h)
                cond = _make_eq_expr(col, val)
                conds.append(cond)

        for val_h in unique_dropped:
            if val_h not in unique_kept:
                val = _unhashable(val_h)
                cond = _make_neq_expr(col, val)
                conds.append(cond)

        kept_nums = [try_numeric(v) for v in kept_vals]
        dropped_nums = [try_numeric(v) for v in dropped_vals]
        all_kept_numeric = all(is_num for _, is_num in kept_nums)
        all_dropped_numeric = all(is_num for _, is_num in dropped_nums)

        if all_kept_numeric and all_dropped_numeric:
            kn = [n for n, _ in kept_nums]
            dn = [n for n, _ in dropped_nums]
            if kn and dn:
                min_k, max_k = min(kn), max(kn)
                min_d, max_d = min(dn), max(dn)

                for threshold in [min_k, max_k, min_d, max_d]:
                    if threshold == int(threshold):
                        threshold = int(threshold)
                    for cmp_type in ['>=', '>', '<=', '<']:
                        if cmp_type == '>=' and min_d < min_k:
                            cond = _make_cmp_expr(col, '>=', threshold)
                            conds.append(cond)
                        elif cmp_type == '>' and min_d < min_k:
                            cond = _make_cmp_expr(col, '>', threshold)
                            conds.append(cond)
                        elif cmp_type == '<=' and max_d > max_k:
                            cond = _make_cmp_expr(col, '<=', threshold)
                            conds.append(cond)
                        elif cmp_type == '<' and max_d > max_k:
                            cond = _make_cmp_expr(col, '<', threshold)
                            conds.append(cond)
    return conds


def _hashable(v):
    if v is None:
        return ("__none__",)
    if isinstance(v, bool):
        return ("__bool__", v)
    if isinstance(v, (int, float)):
        return ("__num__", v)
    return ("__str__", str(v))


def _unhashable(h):
    tag = h[0]
    if tag == "__none__":
        return None
    if tag == "__bool__":
        return h[1]
    if tag == "__num__":
        return h[1]
    return h[1]


def _check_condition(
    cond: str, kept_n: list[dict], dropped_n: list[dict], all_columns: list[str]
) -> bool:
    """Check that condition is True for all kept rows and False for all dropped rows."""
    try:
        for row in kept_n:
            ns = _make_namespace(row, all_columns)
            if not eval(cond, {"__builtins__": {}}, ns):
                return False
        for row in dropped_n:
            ns = _make_namespace(row, all_columns)
            if eval(cond, {"__builtins__": {}}, ns):
                return False
    except Exception:
        return False
    return True


def _make_namespace(row: dict, all_columns: list[str]) -> dict:
    """Create a namespace dict for eval(), mapping column names to parsed values."""
    ns = {"_parsed": {}}
    for col in all_columns:
        val = row.get(col)
        ns["_parsed"][col] = val
    return ns


def _python_val_repr(v: Any) -> str:
    """Return Python literal representation of a value."""
    if v is None:
        return "None"
    if isinstance(v, bool):
        return "True" if v else "False"
    if isinstance(v, int):
        return repr(v)
    if isinstance(v, float):
        if v == int(v) and not math.isinf(v):
            return repr(int(v))
        return repr(v)
    return repr(str(v))


def _python_val_repr_raw(v: Any) -> str:
    """Return Python literal representation keeping float as float."""
    if v is None:
        return "None"
    if isinstance(v, bool):
        return "True" if v else "False"
    if isinstance(v, int):
        return repr(v)
    if isinstance(v, float):
        return repr(v)
    return repr(str(v))


def _make_eq_expr(col: str, val: Any) -> str:
    return f"_parsed[{repr(col)}] == {_python_val_repr(val)}"


def _make_neq_expr(col: str, val: Any) -> str:
    return f"_parsed[{repr(col)}] != {_python_val_repr(val)}"


def _make_gte_expr(col: str, threshold) -> str:
    return f"_try_float(_parsed[{repr(col)}]) >= {_python_val_repr(threshold)}"


def _make_gt_expr(col: str, threshold) -> str:
    return f"_try_float(_parsed[{repr(col)}]) > {_python_val_repr(threshold)}"


def _make_lte_expr(col: str, threshold) -> str:
    return f"_try_float(_parsed[{repr(col)}]) <= {_python_val_repr(threshold)}"


def _make_lt_expr(col: str, threshold) -> str:
    return f"_try_float(_parsed[{repr(col)}]) < {_python_val_repr(threshold)}"


def _make_cmp_expr(col: str, op: str, threshold) -> str:
    return f"_try_float(_parsed[{repr(col)}]) {op} {_python_val_repr(threshold)}"


# ── Stateful Transform Inference ────────────────────────────────────────

def infer_stateful_transforms(
    input_rows: list[dict],
    output_rows: list[dict],
    kept_indices: list[int],
    col_transforms: dict
) -> dict:
    """
    Infer stateful transformations (prefix, sliding window, state machine).

    Only processes columns that were marked as "stateful" in col_transforms
    (i.e., not already explained by simple per-row transforms).

    Returns a dict with:
    - prefix_transforms: list of prefix transform descriptors
    - window_transforms: list of sliding window transform descriptors
    - state_transforms: list of state machine transform descriptors
    """
    result = {
        "prefix_transforms": [],
        "window_transforms": [],
        "state_transforms": [],
    }

    if not output_rows:
        return result

    input_cols = list(input_rows[0].keys())
    output_cols = list(output_rows[0].keys())

    # Only check columns that weren't explained by simple transforms
    stateful_cols = [
        ocol for ocol in output_cols
        if col_transforms.get(ocol, {}).get("type") == "stateful"
    ]

    # For each output column marked as stateful, check stateful transforms
    for ocol in stateful_cols:
        out_vals = [parse_value(r[ocol]) for r in output_rows]

        # Try prefix transforms
        prefix_t = _try_prefix_transform(ocol, out_vals, input_rows, kept_indices, input_cols)
        if prefix_t:
            result["prefix_transforms"].append(prefix_t)
            continue

        # Try sliding window transforms
        window_t = _try_window_transform(ocol, out_vals, input_rows, kept_indices, input_cols)
        if window_t:
            result["window_transforms"].append(window_t)
            continue

        # Try state machine transforms
        state_t = _try_state_transform(ocol, out_vals, input_rows, kept_indices, input_cols)
        if state_t:
            result["state_transforms"].append(state_t)
            continue

    return result


def _try_prefix_transform(
    ocol: str,
    out_vals: list[Any],
    input_rows: list[dict],
    kept_indices: list[int],
    input_cols: list[str]
) -> dict | None:
    """Try to detect a prefix/cumulative transform (sum, count, avg)."""
    # Check if output values are numeric
    out_nums = []
    for v in out_vals:
        n, ok = try_numeric(v)
        if not ok:
            return None
        out_nums.append(n)

    if len(out_nums) < 2:
        return None

    # For each input column, check prefix patterns
    for icol in input_cols:
        in_vals = []
        for ki in kept_indices:
            v = parse_value(input_rows[ki].get(icol))
            n, ok = try_numeric(v)
            if not ok:
                break
            in_vals.append(n)
        else:
            if len(in_vals) != len(out_nums):
                continue

            # Check for prefix sum: out[i] = a * (in[0] + ... + in[i]) + b
            prefix_sum = _try_prefix_sum(icol, in_vals, out_nums)
            if prefix_sum:
                prefix_sum["output_col"] = ocol
                return prefix_sum

            # Check for prefix count (row count based)
            prefix_count = _try_prefix_count(icol, in_vals, out_nums)
            if prefix_count:
                prefix_count["output_col"] = ocol
                return prefix_count

    return None


def _try_prefix_sum(icol: str, in_vals: list[float], out_vals: list[float]) -> dict | None:
    """Check if out[i] = a * sum(in[0:i+1]) + b."""
    # Compute prefix sums
    prefix_sums = []
    running = 0.0
    for v in in_vals:
        running += v
        prefix_sums.append(running)

    # Try to find linear relationship: out = a * prefix_sum + b
    if len(prefix_sums) >= 2:
        # Solve for a and b using first two points
        s1, s2 = prefix_sums[0], prefix_sums[1]
        o1, o2 = out_vals[0], out_vals[1]

        if abs(s2 - s1) < 1e-12:
            # Check if outputs are also constant
            if abs(o2 - o1) < 1e-12 and all(abs(out_vals[i] - o1) < 1e-9 for i in range(len(out_vals))):
                return {"type": "prefix_sum", "source": icol, "a": 0.0, "b": o1}
            return None

        a = (o2 - o1) / (s2 - s1)
        b = o1 - a * s1

        # Verify for all points
        for i in range(len(out_vals)):
            expected = a * prefix_sums[i] + b
            if abs(expected - out_vals[i]) > 1e-6:
                return None

        # Clean up a and b
        a_clean = int(a) if abs(a - int(a)) < 1e-9 else a
        b_clean = int(b) if abs(b - int(b)) < 1e-9 else b

        return {"type": "prefix_sum", "source": icol, "a": a_clean, "b": b_clean}

    return None


def _try_prefix_count(icol: str, in_vals: list[float], out_vals: list[float]) -> dict | None:
    """Check if out[i] = a * (i+1) + b (prefix count/row index based)."""
    # Compute prefix counts (row indices + 1)
    prefix_counts = [i + 1 for i in range(len(out_vals))]

    # Try to find linear relationship: out = a * count + b
    if len(prefix_counts) >= 2:
        c1, c2 = prefix_counts[0], prefix_counts[1]
        o1, o2 = out_vals[0], out_vals[1]

        a = (o2 - o1) / (c2 - c1)  # c2 - c1 is always 1
        b = o1 - a * c1

        # Verify for all points
        for i in range(len(out_vals)):
            expected = a * prefix_counts[i] + b
            if abs(expected - out_vals[i]) > 1e-6:
                return None

        a_clean = int(a) if abs(a - int(a)) < 1e-9 else a
        b_clean = int(b) if abs(b - int(b)) < 1e-9 else b

        return {"type": "prefix_count", "a": a_clean, "b": b_clean}

    return None


def _try_window_transform(
    ocol: str,
    out_vals: list[Any],
    input_rows: list[dict],
    kept_indices: list[int],
    input_cols: list[str]
) -> dict | None:
    """Try to detect a sliding window transform (running mean, sum, count)."""
    # Check if output values are numeric
    out_nums = []
    for v in out_vals:
        n, ok = try_numeric(v)
        if not ok:
            return None
        out_nums.append(n)

    if len(out_nums) < 2:
        return None

    # For each input column, try window transforms
    for icol in input_cols:
        in_vals = []
        for ki in kept_indices:
            v = parse_value(input_rows[ki].get(icol))
            n, ok = try_numeric(v)
            if not ok:
                break
            in_vals.append(n)
        else:
            if len(in_vals) != len(out_nums):
                continue

            # Try different window sizes (1 to min(64, len(in_vals)))
            max_window = min(64, len(in_vals))
            for w in range(1, max_window + 1):
                window_t = _try_window_size(icol, in_vals, out_nums, w)
                if window_t:
                    window_t["output_col"] = ocol
                    return window_t

    return None


def _try_window_size(icol: str, in_vals: list[float], out_vals: list[float], window_size: int) -> dict | None:
    """Check if out values are computed from a sliding window of given size."""
    n = len(in_vals)

    # For each row, compute window sum and count
    window_sums = []
    window_counts = []

    for i in range(n):
        # Window covers rows [max(0, i-w+1), i]
        start = max(0, i - window_size + 1)
        window_sum = sum(in_vals[start:i+1])
        window_count = i - start + 1
        window_sums.append(window_sum)
        window_counts.append(window_count)

    # Try: out = a * window_sum + b
    result = _fit_linear(window_sums, out_vals)
    if result:
        a, b = result
        return {"type": "window_sum", "source": icol, "window_size": window_size, "a": a, "b": b}

    # Try: out = a * window_mean + b
    window_means = [window_sums[i] / window_counts[i] for i in range(n)]
    result = _fit_linear(window_means, out_vals)
    if result:
        a, b = result
        return {"type": "window_mean", "source": icol, "window_size": window_size, "a": a, "b": b}

    # Try: out = a * window_count + b
    result = _fit_linear(window_counts, out_vals)
    if result:
        a, b = result
        return {"type": "window_count", "source": icol, "window_size": window_size, "a": a, "b": b}

    return None


def _fit_linear(x_vals: list[float], y_vals: list[float]) -> tuple[float, float] | None:
    """Fit y = a*x + b to the data, return (a, b) or None if no good fit."""
    if len(x_vals) < 2:
        return None

    # Try to find a pair of points with different x values
    for i in range(len(x_vals)):
        for j in range(i + 1, len(x_vals)):
            x1, x2 = x_vals[i], x_vals[j]
            y1, y2 = y_vals[i], y_vals[j]

            if abs(x2 - x1) < 1e-12:
                continue  # Skip pairs with same x value

            a = (y2 - y1) / (x2 - x1)
            b = y1 - a * x1

            # Verify against all points
            valid = True
            for k in range(len(x_vals)):
                expected = a * x_vals[k] + b
                if abs(expected - y_vals[k]) > 1e-6:
                    valid = False
                    break

            if valid:
                a_clean = int(a) if abs(a - int(a)) < 1e-9 else a
                b_clean = int(b) if abs(b - int(b)) < 1e-9 else b
                return (a_clean, b_clean)

    # No pair with different x values found - check if all y values are the same
    if len(y_vals) > 0:
        y0 = y_vals[0]
        if all(abs(y - y0) < 1e-9 for y in y_vals):
            return (0.0, y0)

    return None


def _try_state_transform(
    ocol: str,
    out_vals: list[Any],
    input_rows: list[dict],
    kept_indices: list[int],
    input_cols: list[str]
) -> dict | None:
    """Try to detect a state machine transform (segment_id, mode switching)."""
    # Get unique output values
    unique_out = set(out_vals)
    if len(unique_out) > 16:  # Too many states
        return None

    if len(unique_out) < 2:
        return None  # No state changes

    # Try to detect state transitions based on input column conditions
    for icol in input_cols:
        in_vals = [parse_value(input_rows[ki].get(icol)) for ki in kept_indices]

        # Try to detect threshold-based state transitions
        state_t = _detect_threshold_states(ocol, in_vals, out_vals, icol)
        if state_t:
            return state_t

        # Try to detect value-change-based state transitions
        state_t = _detect_value_change_states(ocol, in_vals, out_vals, icol)
        if state_t:
            return state_t

    return None


def _detect_threshold_states(ocol: str, in_vals: list[Any], out_vals: list[Any], icol: str) -> dict | None:
    """Detect state transitions based on threshold crossing."""
    # Try numeric thresholds
    in_nums = []
    for v in in_vals:
        n, ok = try_numeric(v)
        if not ok:
            return None
        in_nums.append(n)

    unique_states = list(dict.fromkeys(out_vals))  # Preserve order

    # Try to find thresholds that explain state transitions
    # For simplicity, detect if state changes when crossing a threshold
    # State increments by 1 each time we cross the threshold in a specific direction

    # Check for "state increments when value crosses threshold" pattern
    for threshold in sorted(set(in_nums)):
        # Check if state increments when crossing from below to above threshold
        state = 1
        states_computed = []
        prev_above = None

        for v in out_vals:
            states_computed.append(v)

        # Check simple pattern: state = initial + number of threshold crossings
        computed_states = []
        state_id = 1
        prev_val = None
        prev_was_below = None

        for i, v in enumerate(in_nums):
            is_above = v >= threshold

            if prev_was_below is not None:
                # Crossing from below to above increments state
                if prev_was_below and is_above:
                    state_id += 1
                # Crossing from above to below also increments (for segment patterns)
                elif not prev_was_below and not is_above:
                    state_id += 1

            computed_states.append(state_id)
            prev_was_below = not is_above if prev_was_below is None else (v < threshold)

        # Actually, let's try a simpler approach: state changes when condition changes
        computed_states = []
        state_id = 1
        prev_condition = None

        for i, v in enumerate(in_nums):
            condition = v >= threshold

            if prev_condition is not None and condition != prev_condition:
                state_id += 1

            computed_states.append(state_id)
            prev_condition = condition

        # Check if computed states match output
        # But output might be state_id + offset, so try to fit
        out_nums = [try_numeric(v)[0] for v in out_vals]
        if all(try_numeric(v)[1] for v in out_vals):
            # Try linear fit
            result = _fit_linear(computed_states, out_nums)
            if result:
                a, b = result
                # Verify
                match = True
                for i in range(len(out_vals)):
                    expected = a * computed_states[i] + b
                    if abs(expected - out_nums[i]) > 1e-6:
                        match = False
                        break
                if match:
                    return {
                        "type": "threshold_state",
                        "output_col": ocol,
                        "source": icol,
                        "threshold": threshold,
                        "initial_state": 1,
                        "a": a,
                        "b": b,
                    }

    return None


def _detect_value_change_states(ocol: str, in_vals: list[Any], out_vals: list[Any], icol: str) -> dict | None:
    """Detect state transitions based on value changes."""
    # State increments when input value changes
    computed_states = []
    state_id = 1
    prev_val = None

    for v in in_vals:
        if prev_val is not None and not values_equal(v, prev_val):
            state_id += 1
        computed_states.append(state_id)
        prev_val = v

    # Check if computed states match output (with possible linear transform)
    out_nums = [try_numeric(v)[0] for v in out_vals]
    if all(try_numeric(v)[1] for v in out_vals):
        result = _fit_linear(computed_states, out_nums)
        if result:
            a, b = result
            match = True
            for i in range(len(out_vals)):
                expected = a * computed_states[i] + b
                if abs(expected - out_nums[i]) > 1e-6:
                    match = False
                    break
            if match:
                return {
                    "type": "value_change_state",
                    "output_col": ocol,
                    "source": icol,
                    "initial_state": 1,
                    "a": a,
                    "b": b,
                }

    return None


# ── Neighbor-Based Filtering Inference ──────────────────────────────────

def infer_neighbor_filter(
    input_rows: list[dict],
    output_rows: list[dict]
) -> dict | None:
    """
    Infer neighbor-based filtering rules.

    Returns a dict describing the neighbor filtering rules, or None if not applicable.
    """
    if len(input_rows) == len(output_rows):
        return None

    kept_indices = _match_rows(input_rows, output_rows)
    if len(kept_indices) == len(input_rows):
        return None

    dropped_indices = [i for i in range(len(input_rows)) if i not in kept_indices]

    # Try to detect patterns based on neighboring rows
    input_cols = list(input_rows[0].keys())

    # Pattern 1: Drop row i if next row (i+1) has some property
    lookahead_filter = _detect_lookahead_filter(input_rows, kept_indices, dropped_indices, input_cols)
    if lookahead_filter:
        return lookahead_filter

    # Pattern 2: Drop row i if previous row (i-1) has some property
    lookbehind_filter = _detect_lookbehind_filter(input_rows, kept_indices, dropped_indices, input_cols)
    if lookbehind_filter:
        return lookbehind_filter

    # Pattern 3: Drop duplicates (keep first or last in runs)
    duplicate_filter = _detect_duplicate_filter(input_rows, kept_indices, dropped_indices, input_cols)
    if duplicate_filter:
        return duplicate_filter

    return None


def _detect_lookahead_filter(
    input_rows: list[dict],
    kept_indices: list[int],
    dropped_indices: list[int],
    input_cols: list[str]
) -> dict | None:
    """Detect if dropped rows depend on the next row's properties."""
    # For each dropped row, check if the next row has a consistent property
    for col in input_cols:
        dropped_vals = []
        for i in dropped_indices:
            if i + 1 < len(input_rows):
                next_val = parse_value(input_rows[i + 1].get(col))
                dropped_vals.append(next_val)

        if dropped_vals:
            unique_vals = set(_hashable(v) for v in dropped_vals)
            if len(unique_vals) == 1:
                # All dropped rows have next row with the same value
                val = _unhashable(list(unique_vals)[0])

                # Verify that no kept row has this pattern
                false_positive = False
                for i in kept_indices:
                    if i + 1 < len(input_rows):
                        next_val = parse_value(input_rows[i + 1].get(col))
                        if values_equal(next_val, val):
                            false_positive = True
                            break

                if not false_positive:
                    return {
                        "type": "drop_if_next_equals",
                        "column": col,
                        "value": val,
                    }

    return None


def _detect_lookbehind_filter(
    input_rows: list[dict],
    kept_indices: list[int],
    dropped_indices: list[int],
    input_cols: list[str]
) -> dict | None:
    """Detect if dropped rows depend on the previous row's properties."""
    for col in input_cols:
        # Check: drop if previous row has certain value
        dropped_vals = []
        for i in dropped_indices:
            if i > 0:
                prev_val = parse_value(input_rows[i - 1].get(col))
                dropped_vals.append(prev_val)

        if dropped_vals:
            unique_vals = set(_hashable(v) for v in dropped_vals)
            if len(unique_vals) == 1:
                val = _unhashable(list(unique_vals)[0])

                # Verify
                false_positive = False
                for i in kept_indices:
                    if i > 0:
                        prev_val = parse_value(input_rows[i - 1].get(col))
                        if values_equal(prev_val, val):
                            false_positive = True
                            break

                if not false_positive:
                    return {
                        "type": "drop_if_prev_equals",
                        "column": col,
                        "value": val,
                    }

    return None


def _detect_duplicate_filter(
    input_rows: list[dict],
    kept_indices: list[int],
    dropped_indices: list[int],
    input_cols: list[str]
) -> dict | None:
    """Detect duplicate handling patterns (keep first/last in runs)."""
    for col in input_cols:
        vals = [parse_value(r.get(col)) for r in input_rows]

        # Check for runs of equal values
        runs = []
        current_run = [0]
        for i in range(1, len(vals)):
            if values_equal(vals[i], vals[i-1]):
                current_run.append(i)
            else:
                if len(current_run) > 1:
                    runs.append(current_run)
                current_run = [i]
        if len(current_run) > 1:
            runs.append(current_run)

        if runs:
            # Check if only first of each run is kept
            keep_first = True
            for run in runs:
                if run[0] not in kept_indices:
                    keep_first = False
                    break
                for idx in run[1:]:
                    if idx in kept_indices:
                        keep_first = False
                        break

            if keep_first:
                return {
                    "type": "keep_first_in_run",
                    "column": col,
                }

            # Check if only last of each run is kept
            keep_last = True
            for run in runs:
                if run[-1] not in kept_indices:
                    keep_last = False
                    break
                for idx in run[:-1]:
                    if idx in kept_indices:
                        keep_last = False
                        break

            if keep_last:
                return {
                    "type": "keep_last_in_run",
                    "column": col,
                }

    return None


# ── Column Transformation Inference ────────────────────────────────────

def infer_transformations(
    input_rows: list[dict], output_rows: list[dict], kept_indices: list[int]
) -> dict:
    """
    Infer column-level transformations from input to output.

    Returns a dict describing:
    - output_columns: list of output column names in order
    - column_transforms: dict mapping output_col -> transform descriptor
    """
    input_cols = list(input_rows[0].keys())
    output_cols = list(output_rows[0].keys()) if output_rows else []

    if not output_rows:
        return {"output_columns": [], "column_transforms": {}}

    transforms = {}

    for ocol in output_cols:
        out_vals = [parse_value(r[ocol]) for r in output_rows]
        transform = _infer_single_transform(
            ocol, out_vals, input_rows, kept_indices, input_cols
        )
        transforms[ocol] = transform

    return {"output_columns": output_cols, "column_transforms": transforms}


def _infer_single_transform(
    ocol: str,
    out_vals: list[Any],
    input_rows: list[dict],
    kept_indices: list[int],
    input_cols: list[str],
) -> dict:
    """Infer the transformation for a single output column."""
    # Check 1: Same column name, same values -> identity
    if ocol in input_cols:
        in_vals = [parse_value(input_rows[i][ocol]) for i in kept_indices]
        if all(values_equal(a, b) for a, b in zip(in_vals, out_vals)):
            return {"type": "identity", "source": ocol}

    # Check 2: Renamed from another column (same values)
    for icol in input_cols:
        if icol == ocol:
            continue
        in_vals = [parse_value(input_rows[i][icol]) for i in kept_indices]
        if all(values_equal(a, b) for a, b in zip(in_vals, out_vals)):
            return {"type": "rename", "source": icol}

    # Check 3: Constant value
    first = out_vals[0]
    if all(values_equal(v, first) for v in out_vals):
        return {"type": "constant", "value": first}

    # Check 4: Numeric linear transform from any input column
    best_transform = None
    best_score = float("inf")
    for icol in input_cols:
        transform = _try_linear_transform(ocol, out_vals, input_rows, kept_indices, icol)
        if transform is not None:
            # Prefer simpler source columns (same name first)
            score = 0 if icol == ocol else 1
            if score < best_score:
                best_score = score
                best_transform = transform
    if best_transform is not None:
        return best_transform

    # Check 5: String transforms
    for icol in input_cols:
        transform = _try_string_transform(ocol, out_vals, input_rows, kept_indices, icol)
        if transform is not None:
            return transform

    # Check 6: Splitting
    for icol in input_cols:
        transform = _try_split_transform(ocol, out_vals, input_rows, kept_indices, icol)
        if transform is not None:
            return transform

    # Check 7: Prefix/Suffix
    for icol in input_cols:
        transform = _try_prefix_suffix_transform(ocol, out_vals, input_rows, kept_indices, icol)
        if transform is not None:
            return transform

    # Check 8: Combining two columns
    transform = _try_combine_transform(ocol, out_vals, input_rows, kept_indices, input_cols)
    if transform is not None:
        return transform

    # Fallback: mark as stateful (will be filled by stateful inference)
    return {"type": "stateful", "output_col": ocol}


def _try_linear_transform(
    ocol: str,
    out_vals: list[Any],
    input_rows: list[dict],
    kept_indices: list[int],
    icol: str,
) -> dict | None:
    """Try to find a linear transform y = a*x + b."""
    pairs = []
    for idx, ki in enumerate(kept_indices):
        in_raw = parse_value(input_rows[ki].get(icol))
        out_val = out_vals[idx]
        in_num, in_ok = try_numeric(in_raw)
        out_num, out_ok = try_numeric(out_val)
        if in_ok and out_ok:
            pairs.append((float(in_num), float(out_num)))

    if len(pairs) < 2:
        return None

    # Solve for a and b using first two pairs
    x1, y1 = pairs[0]
    x2, y2 = pairs[1]

    if abs(x2 - x1) < 1e-12:
        if abs(y2 - y1) < 1e-12:
            return None
        return None

    a = (y2 - y1) / (x2 - x1)
    b = y1 - a * x1

    for x, y in pairs:
        expected = a * x + b
        if abs(expected - y) > 1e-6:
            return None

    if abs(a - 1.0) < 1e-9 and abs(b) < 1e-9:
        return {"type": "identity", "source": icol} if icol == ocol else {"type": "rename", "source": icol}

    if abs(a) < 1e-9:
        return {"type": "constant", "value": b if b != int(b) else int(b)}

    a_clean = int(a) if a == int(a) else a
    b_clean = int(b) if b == int(b) else b

    return {
        "type": "linear",
        "source": icol,
        "a": a_clean,
        "b": b_clean,
    }


def _try_string_transform(
    ocol: str,
    out_vals: list[Any],
    input_rows: list[dict],
    kept_indices: list[int],
    icol: str,
) -> dict | None:
    """Try to detect string transformations (trim, lower, upper)."""
    in_strs = []
    out_strs = []
    for idx, ki in enumerate(kept_indices):
        in_raw = parse_value(input_rows[ki].get(icol))
        out_val = out_vals[idx]
        if in_raw is None or out_val is None:
            return None
        in_str = str(in_raw)
        out_str = str(out_val)
        in_strs.append(in_str)
        out_strs.append(out_str)

    # Check lower
    if all(in_str.lower() == out_str for in_str, out_str in zip(in_strs, out_strs)):
        return {"type": "string_lower", "source": icol}

    # Check upper
    if all(in_str.upper() == out_str for in_str, out_str in zip(in_strs, out_strs)):
        return {"type": "string_upper", "source": icol}

    # Check trim
    if all(in_str.strip() == out_str for in_str, out_str in zip(in_strs, out_strs)):
        return {"type": "string_trim", "source": icol}

    # Check trim + lower
    if all(in_str.strip().lower() == out_str for in_str, out_str in zip(in_strs, out_strs)):
        return {"type": "string_trim_lower", "source": icol}

    # Check trim + upper
    if all(in_str.strip().upper() == out_str for in_str, out_str in zip(in_strs, out_strs)):
        return {"type": "string_trim_upper", "source": icol}

    return None


def _try_split_transform(
    ocol: str,
    out_vals: list[Any],
    input_rows: list[dict],
    kept_indices: list[int],
    icol: str,
) -> dict | None:
    """Try to detect splitting of a column."""
    for sep in [" ", ",", ";", "-", "_", "|", "/"]:
        parts_list = []
        for idx, ki in enumerate(kept_indices):
            in_raw = parse_value(input_rows[ki].get(icol))
            if in_raw is None:
                break
            parts = str(in_raw).split(sep)
            parts_list.append((idx, parts))
        else:
            # Check if any part index consistently gives the output
            for part_idx in range(max(len(p) for _, p in parts_list)):
                match = True
                for idx, parts in parts_list:
                    if part_idx >= len(parts):
                        match = False
                        break
                    out_val = out_vals[idx]
                    if out_val is None:
                        match = False
                        break
                    if not values_equal(parse_value(parts[part_idx].strip()), out_val):
                        match = False
                        break
                if match:
                    return {
                        "type": "split",
                        "source": icol,
                        "separator": sep,
                        "part_index": part_idx,
                        "strip": True,
                    }
    return None


def _try_prefix_suffix_transform(
    ocol: str,
    out_vals: list[Any],
    input_rows: list[dict],
    kept_indices: list[int],
    icol: str,
) -> dict | None:
    """Try to detect prefix/suffix additions."""
    in_strs = []
    out_strs = []
    for idx, ki in enumerate(kept_indices):
        in_raw = parse_value(input_rows[ki].get(icol))
        out_val = out_vals[idx]
        if in_raw is None or out_val is None:
            return None
        in_strs.append(str(in_raw))
        out_strs.append(str(out_val))

    if not in_strs:
        return None

    # Check prefix
    prefixes = set()
    for in_s, out_s in zip(in_strs, out_strs):
        if out_s.startswith(in_s):
            prefix = out_s[: len(out_s) - len(in_s)]
            prefixes.add(prefix)
        elif out_s.startswith(in_s.upper()):
            # might be upper + prefix
            return None
        else:
            return None

    if len(prefixes) == 1:
        prefix = prefixes.pop()
        if prefix:
            return {"type": "add_prefix", "source": icol, "prefix": prefix}

    # Check suffix
    suffixes = set()
    for in_s, out_s in zip(in_strs, out_strs):
        if out_s.endswith(in_s):
            suffix = out_s[len(in_s):]
            suffixes.add(suffix)
        else:
            return None

    if len(suffixes) == 1:
        suffix = suffixes.pop()
        if suffix:
            return {"type": "add_suffix", "source": icol, "suffix": suffix}

    return None


def _try_combine_transform(
    ocol: str,
    out_vals: list[Any],
    input_rows: list[dict],
    kept_indices: list[int],
    input_cols: list[str],
) -> dict | None:
    """Try to detect combining two columns with a separator."""
    for i, icol1 in enumerate(input_cols):
        for icol2 in input_cols[i+1:]:
            for sep in [" ", ",", ";", "-", "_", "|", "/", ""]:
                match = True
                for idx, ki in enumerate(kept_indices):
                    v1 = parse_value(input_rows[ki].get(icol1))
                    v2 = parse_value(input_rows[ki].get(icol2))
                    out_val = out_vals[idx]
                    if v1 is None or v2 is None or out_val is None:
                        match = False
                        break
                    combined = str(v1) + sep + str(v2)
                    if combined != str(out_val):
                        match = False
                        break
                if match:
                    return {
                        "type": "combine",
                        "source1": icol1,
                        "source2": icol2,
                        "separator": sep,
                    }
    return None


# ── Filter Inference Helper ────────────────────────────────────────────

def infer_filter_and_match(
    input_rows: list[dict], output_rows: list[dict]
) -> tuple[str | None, list[int]]:
    """Infer filter condition and return matched indices."""
    if len(input_rows) == len(output_rows):
        # Check if they're all the same
        all_same = True
        for in_row, out_row in zip(input_rows, output_rows):
            for key in out_row:
                if key in in_row:
                    if not values_equal(parse_value(in_row[key]), parse_value(out_row[key])):
                        all_same = False
                        break
            if not all_same:
                break
        if all_same:
            return None, list(range(len(input_rows)))

    kept_indices = _match_rows(input_rows, output_rows)

    if len(kept_indices) == len(input_rows):
        return None, kept_indices

    kept = [input_rows[i] for i in kept_indices]
    dropped = [input_rows[i] for i in range(len(input_rows)) if i not in set(kept_indices)]

    condition = _infer_predicate(kept, dropped, list(input_rows[0].keys()))
    return condition, kept_indices


# ── Python Code Generation ─────────────────────────────────────────────

def generate_python_module(
    module_name: str,
    ext: str,
    filter_condition: str | None,
    transforms: dict,
    stateful_transforms: dict,
    neighbor_filter: dict | None,
) -> str:
    """Generate the Python module source code."""
    output_cols = transforms["output_columns"]
    col_transforms = transforms["column_transforms"]

    lines = []
    lines.append('"""Auto-generated DynamicPreprocessor module."""')
    lines.append('')
    lines.append('import csv')
    lines.append('import json')
    lines.append('import os')
    lines.append('import hashlib')
    lines.append('from collections import deque')
    lines.append('')
    lines.append('')

    # Parse value function
    lines.append('def _parse_value(v):')
    lines.append('    """Parse a raw value to a typed Python primitive."""')
    lines.append('    if v is None:')
    lines.append('        return None')
    lines.append('    if isinstance(v, bool):')
    lines.append('        return v')
    lines.append('    if isinstance(v, (int, float)):')
    lines.append('        return v')
    lines.append('    s = str(v)')
    lines.append('    try:')
    lines.append('        return int(s)')
    lines.append('    except (ValueError, TypeError):')
    lines.append('        pass')
    lines.append('    try:')
    lines.append('        return float(s)')
    lines.append('    except (ValueError, TypeError):')
    lines.append('        pass')
    lines.append('    if s.lower() == "true":')
    lines.append('        return True')
    lines.append('    if s.lower() == "false":')
    lines.append('        return False')
    lines.append('    if s.lower() == "null" or s == "":')
    lines.append('        return None')
    lines.append('    return s')
    lines.append('')
    lines.append('')

    # Try float function
    lines.append('def _try_float(v):')
    lines.append('    """Try to convert value to float for comparison."""')
    lines.append('    if v is None:')
    lines.append('        return None')
    lines.append('    if isinstance(v, bool):')
    lines.append('        return None')
    lines.append('    if isinstance(v, (int, float)):')
    lines.append('        return float(v)')
    lines.append('    try:')
    lines.append('        return float(v)')
    lines.append('    except (ValueError, TypeError):')
    lines.append('        return None')
    lines.append('')
    lines.append('')

    # State class
    lines.append('class _StreamState:')
    lines.append('    """Holds state for stateful transforms."""')
    lines.append('    def __init__(self):')
    lines.append('        self.prefix_sums = {}')
    lines.append('        self.prefix_counts = {}')
    lines.append('        self.windows = {}')
    lines.append('        self.state_values = {}')
    lines.append('        self.prev_values = {}')
    lines.append('        self.prev_row = None')
    lines.append('        self.pending_row = None')
    lines.append('        self.row_index = 0')
    lines.append('')
    lines.append('    def to_dict(self):')
    lines.append('        return {')
    lines.append('            "prefix_sums": dict(self.prefix_sums),')
    lines.append('            "prefix_counts": dict(self.prefix_counts),')
    lines.append('            "windows": {k: list(v) for k, v in self.windows.items()},')
    lines.append('            "state_values": dict(self.state_values),')
    lines.append('            "prev_values": dict(self.prev_values),')
    lines.append('            "row_index": self.row_index,')
    lines.append('        }')
    lines.append('')
    lines.append('    @classmethod')
    lines.append('    def from_dict(cls, d):')
    lines.append('        s = cls()')
    lines.append('        s.prefix_sums = d.get("prefix_sums", {})')
    lines.append('        s.prefix_counts = d.get("prefix_counts", {})')
    lines.append('        s.windows = {k: deque(v) for k, v in d.get("windows", {}).items()}')
    lines.append('        s.state_values = d.get("state_values", {})')
    lines.append('        s.prev_values = d.get("prev_values", {})')
    lines.append('        s.row_index = d.get("row_index", 0)')
    lines.append('        return s')
    lines.append('')
    lines.append('')

    # Generate state initialization
    state_init_lines = _gen_state_init(stateful_transforms, neighbor_filter)
    lines.extend(state_init_lines)
    lines.append('')

    # Generate transform function
    lines.append('def _transform_row(_parsed, _state):')
    lines.append('    """Apply column transformations to a parsed row."""')
    lines.append('    _result = {}')

    # First, update state for stateful transforms
    state_update_lines = _gen_state_update(stateful_transforms)
    lines.extend(state_update_lines)

    # Then generate column transforms
    for ocol in output_cols:
        t = col_transforms[ocol]
        if t.get("type") == "stateful":
            # This column is filled by a stateful transform
            stateful_lines = _gen_stateful_output(ocol, stateful_transforms)
            lines.extend(stateful_lines)
        else:
            lines.extend(_gen_python_transform(ocol, t))

    lines.append('    return _result')
    lines.append('')
    lines.append('')

    # Generate filter function
    if filter_condition is not None:
        safe_cond = filter_condition
        lines.append('def _keep_row(_parsed):')
        lines.append('    """Determine if a row should be kept (per-row condition)."""')
        lines.append(f'    return {safe_cond}')
        lines.append('')
        lines.append('')
    else:
        lines.append('def _keep_row(_parsed):')
        lines.append('    return True')
        lines.append('')
        lines.append('')

    # Generate neighbor filter function
    neighbor_filter_lines = _gen_neighbor_filter_function(neighbor_filter)
    lines.extend(neighbor_filter_lines)
    lines.append('')

    lines.append(f'_OUTPUT_COLUMNS = {repr(output_cols)}')
    lines.append('')

    lines.append(f'_FILE_EXT = {repr(ext)}')
    lines.append('')
    lines.append('')

    # DynamicPreprocessor class
    lines.append('class DynamicPreprocessor:')
    lines.append('    def __init__(self, buffer, cache_dir=None):')
    lines.append('        self.buffer = buffer')
    lines.append('        self.cache_dir = cache_dir')
    lines.append('')
    lines.append('    def __call__(self, path):')
    lines.append('        return _iterate(path, self.buffer, self.cache_dir)')
    lines.append('')
    lines.append('')

    # Iterator function
    lines.append('def _iterate(path, buffer, cache_dir):')
    lines.append('    ext = _FILE_EXT')
    lines.append('    cache_key = hashlib.sha256(os.path.abspath(path).encode("utf-8")).hexdigest()')
    lines.append('    cache_file = None')
    lines.append('    state_file = None')
    lines.append('    resume_from = 0')
    lines.append('    _state = _StreamState()')
    lines.append('')
    lines.append('    if cache_dir is not None:')
    lines.append('        os.makedirs(cache_dir, exist_ok=True)')
    lines.append('        cache_file = os.path.join(cache_dir, f"cache_{cache_key}")')
    lines.append('        state_file = cache_file + ".state"')
    lines.append('        index_file = cache_file + ".idx"')
    lines.append('        if os.path.isfile(index_file):')
    lines.append('            with open(index_file, "r") as f:')
    lines.append('                resume_from = int(f.read().strip())')
    lines.append('        if os.path.isfile(state_file):')
    lines.append('            try:')
    lines.append('                with open(state_file, "r") as f:')
    lines.append('                    _state = _StreamState.from_dict(json.load(f))')
    lines.append('            except Exception:')
    lines.append('                pass')
    lines.append('')
    lines.append('    row_count = 0')
    lines.append('    buffer_count = 0')
    lines.append('    _pending_rows = []')
    lines.append('')
    lines.append('    for raw_row in _read_rows(path, ext):')
    lines.append('        _parsed = {}')
    lines.append('        for _k, _v in raw_row.items():')
    lines.append('            _parsed[_k] = _parse_value(_v)')
    lines.append('')
    lines.append('        # Per-row filtering')
    lines.append('        if not _keep_row(_parsed):')
    lines.append('            _state.row_index += 1')
    lines.append('            continue')
    lines.append('')
    lines.append('        # Neighbor-based filtering')
    lines.append('        if not _neighbor_keep_row(_parsed, _state):')
    lines.append('            _state.row_index += 1')
    lines.append('            continue')
    lines.append('')
    lines.append('        _result = _transform_row(_parsed, _state)')
    lines.append('        row_count += 1')
    lines.append('        buffer_count += 1')
    lines.append('        _state.row_index += 1')
    lines.append('')
    lines.append('        if row_count <= resume_from:')
    lines.append('            continue')
    lines.append('')
    lines.append('        if cache_dir is not None and state_file is not None:')
    lines.append('            with open(state_file, "w") as f:')
    lines.append('                json.dump(_state.to_dict(), f)')
    lines.append('            with open(cache_file + ".idx", "w") as f:')
    lines.append('                f.write(str(row_count))')
    lines.append('')
    lines.append('        yield _result')
    lines.append('')
    lines.append('        if buffer is not None and buffer_count >= buffer:')
    lines.append('            buffer_count = 0')
    lines.append('')
    lines.append('')

    # Reader function
    lines.append('def _read_rows(path, ext):')
    lines.append('    """Read rows from a file, yielding dicts."""')
    lines.append('    if ext == "csv":')
    lines.append('        with open(path, "r", encoding="utf-8", newline="") as f:')
    lines.append('            reader = csv.DictReader(f)')
    lines.append('            for row in reader:')
    lines.append('                yield dict(row)')
    lines.append('    elif ext == "tsv":')
    lines.append('        with open(path, "r", encoding="utf-8", newline="") as f:')
    lines.append('            reader = csv.DictReader(f, delimiter="\\t")')
    lines.append('            for row in reader:')
    lines.append('                yield dict(row)')
    lines.append('    elif ext == "jsonl":')
    lines.append('        with open(path, "r", encoding="utf-8") as f:')
    lines.append('            for line in f:')
    lines.append('                line = line.strip()')
    lines.append('                if line:')
    lines.append('                    yield json.loads(line)')
    lines.append('    elif ext == "json":')
    lines.append('        with open(path, "r", encoding="utf-8") as f:')
    lines.append('            data = json.load(f)')
    lines.append('            for row in data:')
    lines.append('                yield row')
    lines.append('')
    lines.append('')

    return '\n'.join(lines)


def _gen_state_init(stateful_transforms: dict, neighbor_filter: dict | None) -> list[str]:
    """Generate state initialization constants."""
    lines = []
    lines.append('# Stateful transform configuration')

    # Prefix transforms
    for t in stateful_transforms.get("prefix_transforms", []):
        ocol = t["output_col"]
        source = t["source"]
        t_type = t["type"]
        a = t.get("a", 1)
        b = t.get("b", 0)
        lines.append(f'_PREFIX_{ocol.upper()}_SOURCE = {repr(source)}')
        lines.append(f'_PREFIX_{ocol.upper()}_TYPE = {repr(t_type)}')
        lines.append(f'_PREFIX_{ocol.upper()}_A = {repr(a)}')
        lines.append(f'_PREFIX_{ocol.upper()}_B = {repr(b)}')

    # Window transforms
    for t in stateful_transforms.get("window_transforms", []):
        ocol = t["output_col"]
        source = t["source"]
        t_type = t["type"]
        w = t["window_size"]
        a = t.get("a", 1)
        b = t.get("b", 0)
        lines.append(f'_WINDOW_{ocol.upper()}_SOURCE = {repr(source)}')
        lines.append(f'_WINDOW_{ocol.upper()}_TYPE = {repr(t_type)}')
        lines.append(f'_WINDOW_{ocol.upper()}_SIZE = {w}')
        lines.append(f'_WINDOW_{ocol.upper()}_A = {repr(a)}')
        lines.append(f'_WINDOW_{ocol.upper()}_B = {repr(b)}')

    # State transforms
    for t in stateful_transforms.get("state_transforms", []):
        ocol = t["output_col"]
        source = t.get("source", "")
        t_type = t["type"]
        threshold = t.get("threshold")
        initial = t.get("initial_state", 1)
        a = t.get("a", 1)
        b = t.get("b", 0)
        lines.append(f'_STATE_{ocol.upper()}_SOURCE = {repr(source)}')
        lines.append(f'_STATE_{ocol.upper()}_TYPE = {repr(t_type)}')
        if threshold is not None:
            lines.append(f'_STATE_{ocol.upper()}_THRESHOLD = {repr(threshold)}')
        lines.append(f'_STATE_{ocol.upper()}_INITIAL = {initial}')
        lines.append(f'_STATE_{ocol.upper()}_A = {repr(a)}')
        lines.append(f'_STATE_{ocol.upper()}_B = {repr(b)}')

    # Neighbor filter
    if neighbor_filter:
        nf_type = neighbor_filter["type"]
        nf_col = neighbor_filter.get("column", "")
        nf_val = neighbor_filter.get("value")
        lines.append(f'_NEIGHBOR_FILTER_TYPE = {repr(nf_type)}')
        lines.append(f'_NEIGHBOR_FILTER_COL = {repr(nf_col)}')
        if nf_val is not None:
            lines.append(f'_NEIGHBOR_FILTER_VAL = {_python_val_repr(nf_val)}')
        else:
            lines.append(f'_NEIGHBOR_FILTER_VAL = None')
    else:
        lines.append('_NEIGHBOR_FILTER_TYPE = None')

    return lines


def _gen_state_update(stateful_transforms: dict) -> list[str]:
    """Generate state update code for stateful transforms."""
    lines = []

    # Prefix transforms
    for t in stateful_transforms.get("prefix_transforms", []):
        ocol = t["output_col"]
        source = t["source"]
        lines.append(f'    # Update prefix sum for {ocol}')
        lines.append(f'    _val_{ocol} = _try_float(_parsed.get({repr(source)})) or 0.0')
        lines.append(f'    if {repr(ocol)} not in _state.prefix_sums:')
        lines.append(f'        _state.prefix_sums[{repr(ocol)}] = 0.0')
        lines.append(f'    _state.prefix_sums[{repr(ocol)}] += _val_{ocol}')

    # Window transforms
    for t in stateful_transforms.get("window_transforms", []):
        ocol = t["output_col"]
        source = t["source"]
        w = t["window_size"]
        lines.append(f'    # Update sliding window for {ocol}')
        lines.append(f'    _win_val_{ocol} = _try_float(_parsed.get({repr(source)})) or 0.0')
        lines.append(f'    if {repr(ocol)} not in _state.windows:')
        lines.append(f'        _state.windows[{repr(ocol)}] = deque(maxlen={w})')
        lines.append(f'    _state.windows[{repr(ocol)}].append(_win_val_{ocol})')

    # State transforms
    for t in stateful_transforms.get("state_transforms", []):
        ocol = t["output_col"]
        source = t["source"]
        t_type = t["type"]
        initial = t.get("initial_state", 1)

        if t_type == "threshold_state":
            threshold = t["threshold"]
            lines.append(f'    # Update state machine for {ocol}')
            lines.append(f'    _thresh_val_{ocol} = _try_float(_parsed.get({repr(source)}))')
            lines.append(f'    if {repr(ocol)} not in _state.state_values:')
            lines.append(f'        _state.state_values[{repr(ocol)}] = {initial}')
            lines.append(f'        _state.prev_values[{repr(ocol)}] = (_thresh_val_{ocol} is not None and _thresh_val_{ocol} >= {threshold})')
            lines.append(f'    else:')
            lines.append(f'        _curr_above_{ocol} = _thresh_val_{ocol} is not None and _thresh_val_{ocol} >= {threshold}')
            lines.append(f'        if _state.prev_values[{repr(ocol)}] != _curr_above_{ocol}:')
            lines.append(f'            _state.state_values[{repr(ocol)}] += 1')
            lines.append(f'        _state.prev_values[{repr(ocol)}] = _curr_above_{ocol}')

        elif t_type == "value_change_state":
            lines.append(f'    # Update state machine for {ocol}')
            lines.append(f'    _change_val_{ocol} = _parsed.get({repr(source)})')
            lines.append(f'    if {repr(ocol)} not in _state.state_values:')
            lines.append(f'        _state.state_values[{repr(ocol)}] = {initial}')
            lines.append(f'    else:')
            lines.append(f'        if {repr(ocol)} in _state.prev_values:')
            lines.append(f'            if _change_val_{ocol} != _state.prev_values[{repr(ocol)}]:')
            lines.append(f'                _state.state_values[{repr(ocol)}] += 1')
            lines.append(f'    _state.prev_values[{repr(ocol)}] = _change_val_{ocol}')

    return lines


def _gen_stateful_output(ocol: str, stateful_transforms: dict) -> list[str]:
    """Generate output code for a stateful transform column."""
    lines = []

    # Check prefix transforms
    for t in stateful_transforms.get("prefix_transforms", []):
        if t["output_col"] == ocol:
            t_type = t["type"]
            a = t.get("a", 1)
            b = t.get("b", 0)

            if t_type == "prefix_sum":
                if a == 1 and b == 0:
                    lines.append(f'    _result[{repr(ocol)}] = _state.prefix_sums[{repr(ocol)}]')
                elif a == 1:
                    lines.append(f'    _result[{repr(ocol)}] = _state.prefix_sums[{repr(ocol)}] + {b}')
                elif b == 0:
                    lines.append(f'    _result[{repr(ocol)}] = _state.prefix_sums[{repr(ocol)}] * {a}')
                else:
                    lines.append(f'    _result[{repr(ocol)}] = _state.prefix_sums[{repr(ocol)}] * {a} + {b}')
            elif t_type == "prefix_count":
                lines.append(f'    _prefix_count_{ocol} = _state.row_index + 1')
                if a == 1 and b == 0:
                    lines.append(f'    _result[{repr(ocol)}] = _prefix_count_{ocol}')
                elif a == 1:
                    lines.append(f'    _result[{repr(ocol)}] = _prefix_count_{ocol} + {b}')
                elif b == 0:
                    lines.append(f'    _result[{repr(ocol)}] = _prefix_count_{ocol} * {a}')
                else:
                    lines.append(f'    _result[{repr(ocol)}] = _prefix_count_{ocol} * {a} + {b}')
            return lines

    # Check window transforms
    for t in stateful_transforms.get("window_transforms", []):
        if t["output_col"] == ocol:
            t_type = t["type"]
            a = t.get("a", 1)
            b = t.get("b", 0)
            w = t["window_size"]

            if t_type == "window_sum":
                lines.append(f'    _win_sum_{ocol} = sum(_state.windows[{repr(ocol)}])')
                if a == 1 and b == 0:
                    lines.append(f'    _result[{repr(ocol)}] = _win_sum_{ocol}')
                elif a == 1:
                    lines.append(f'    _result[{repr(ocol)}] = _win_sum_{ocol} + {b}')
                elif b == 0:
                    lines.append(f'    _result[{repr(ocol)}] = _win_sum_{ocol} * {a}')
                else:
                    lines.append(f'    _result[{repr(ocol)}] = _win_sum_{ocol} * {a} + {b}')
            elif t_type == "window_mean":
                lines.append(f'    _win_mean_{ocol} = sum(_state.windows[{repr(ocol)}]) / len(_state.windows[{repr(ocol)}]) if _state.windows[{repr(ocol)}] else 0.0')
                if a == 1 and b == 0:
                    lines.append(f'    _result[{repr(ocol)}] = _win_mean_{ocol}')
                elif a == 1:
                    lines.append(f'    _result[{repr(ocol)}] = _win_mean_{ocol} + {b}')
                elif b == 0:
                    lines.append(f'    _result[{repr(ocol)}] = _win_mean_{ocol} * {a}')
                else:
                    lines.append(f'    _result[{repr(ocol)}] = _win_mean_{ocol} * {a} + {b}')
            elif t_type == "window_count":
                lines.append(f'    _win_count_{ocol} = len(_state.windows[{repr(ocol)}])')
                if a == 1 and b == 0:
                    lines.append(f'    _result[{repr(ocol)}] = _win_count_{ocol}')
                elif a == 1:
                    lines.append(f'    _result[{repr(ocol)}] = _win_count_{ocol} + {b}')
                elif b == 0:
                    lines.append(f'    _result[{repr(ocol)}] = _win_count_{ocol} * {a}')
                else:
                    lines.append(f'    _result[{repr(ocol)}] = _win_count_{ocol} * {a} + {b}')
            return lines

    # Check state transforms
    for t in stateful_transforms.get("state_transforms", []):
        if t["output_col"] == ocol:
            a = t.get("a", 1)
            b = t.get("b", 0)

            if a == 1 and b == 0:
                lines.append(f'    _result[{repr(ocol)}] = _state.state_values[{repr(ocol)}]')
            elif a == 1:
                lines.append(f'    _result[{repr(ocol)}] = _state.state_values[{repr(ocol)}] + {b}')
            elif b == 0:
                lines.append(f'    _result[{repr(ocol)}] = _state.state_values[{repr(ocol)}] * {a}')
            else:
                lines.append(f'    _result[{repr(ocol)}] = _state.state_values[{repr(ocol)}] * {a} + {b}')
            return lines

    lines.append(f'    _result[{repr(ocol)}] = None')
    return lines


def _gen_neighbor_filter_function(neighbor_filter: dict | None) -> list[str]:
    """Generate neighbor-based filter function."""
    lines = []
    lines.append('def _neighbor_keep_row(_parsed, _state):')
    lines.append('    """Determine if a row should be kept based on neighbor conditions."""')

    if neighbor_filter is None:
        lines.append('    return True')
        return lines

    nf_type = neighbor_filter["type"]

    if nf_type == "drop_if_next_equals":
        col = neighbor_filter["column"]
        val = neighbor_filter["value"]
        # For lookahead, we need to defer the decision
        # This is handled differently - we emit a row only after seeing the next row
        lines.append('    # Lookahead filter: will be handled in the iteration')
        lines.append('    # Store current row for deferred decision')
        lines.append('    if _state.pending_row is not None:')
        lines.append(f'        if _parsed.get({repr(col)}) == {_python_val_repr(val)}:')
        lines.append('            # Previous row should be dropped')
        lines.append('            pass')
        lines.append('        else:')
        lines.append('            # Previous row should be kept - but we already yielded it')
        lines.append('            pass')
        lines.append('    _state.pending_row = _parsed')
        lines.append('    # For simplicity, we implement this as a lookbehind check')
        lines.append('    # Check if previous row caused this row to be dropped')
        lines.append(f'    if _state.prev_row is not None and _parsed.get({repr(col)}) == {_python_val_repr(val)}:')
        lines.append('        _state.prev_row = _parsed')
        lines.append('        return False')
        lines.append('    _state.prev_row = _parsed')
        lines.append('    return True')

    elif nf_type == "drop_if_prev_equals":
        col = neighbor_filter["column"]
        val = neighbor_filter["value"]
        lines.append(f'    if _state.prev_row is not None and _state.prev_row.get({repr(col)}) == {_python_val_repr(val)}:')
        lines.append('        _state.prev_row = _parsed')
        lines.append('        return False')
        lines.append('    _state.prev_row = _parsed')
        lines.append('    return True')

    elif nf_type == "keep_first_in_run":
        col = neighbor_filter["column"]
        lines.append(f'    _curr_val = _parsed.get({repr(col)})')
        lines.append(f'    if {repr(col)} in _state.prev_values:')
        lines.append(f'        if _curr_val == _state.prev_values[{repr(col)}]:')
        lines.append(f'            _state.prev_values[{repr(col)}] = _curr_val')
        lines.append('            return False')
        lines.append(f'    _state.prev_values[{repr(col)}] = _curr_val')
        lines.append('    return True')

    elif nf_type == "keep_last_in_run":
        col = neighbor_filter["column"]
        # This requires lookahead, so we defer emission
        lines.append('    # Keep last in run: implemented via lookahead in iteration')
        lines.append('    return True')

    else:
        lines.append('    return True')

    return lines


def _gen_python_transform(ocol: str, t: dict) -> list[str]:
    """Generate Python lines for a single column transform."""
    lines = []
    if t["type"] == "identity":
        lines.append(f'    _result[{repr(ocol)}] = _parsed[{repr(t["source"])}]')
    elif t["type"] == "rename":
        lines.append(f'    _result[{repr(ocol)}] = _parsed[{repr(t["source"])}]')
    elif t["type"] == "constant":
        lines.append(f'    _result[{repr(ocol)}] = {_python_val_repr(t["value"])}')
    elif t["type"] == "linear":
        src = repr(t["source"])
        a = t["a"]
        b = t["b"]
        if a == 1 and b == 0:
            lines.append(f'    _result[{repr(ocol)}] = _parsed[{src}]')
        elif a == 1:
            lines.append(f'    _result[{repr(ocol)}] = _parsed[{src}] + {_python_val_repr(b)}')
        elif a == 0:
            lines.append(f'    _result[{repr(ocol)}] = {_python_val_repr(b)}')
        elif b == 0:
            lines.append(f'    _result[{repr(ocol)}] = _parsed[{src}] * {_python_val_repr_raw(a)}')
        else:
            lines.append(f'    _result[{repr(ocol)}] = _parsed[{src}] * {_python_val_repr_raw(a)} + {_python_val_repr(b)}')
    elif t["type"] == "string_lower":
        lines.append(f'    _result[{repr(ocol)}] = str(_parsed[{repr(t["source"])}]).lower()')
    elif t["type"] == "string_upper":
        lines.append(f'    _result[{repr(ocol)}] = str(_parsed[{repr(t["source"])}]).upper()')
    elif t["type"] == "string_trim":
        lines.append(f'    _result[{repr(ocol)}] = str(_parsed[{repr(t["source"])}]).strip()')
    elif t["type"] == "string_trim_lower":
        lines.append(f'    _result[{repr(ocol)}] = str(_parsed[{repr(t["source"])}]).strip().lower()')
    elif t["type"] == "string_trim_upper":
        lines.append(f'    _result[{repr(ocol)}] = str(_parsed[{repr(t["source"])}]).strip().upper()')
    elif t["type"] == "split":
        src = repr(t["source"])
        sep = repr(t["separator"])
        idx = t["part_index"]
        lines.append(f'    _result[{repr(ocol)}] = str(_parsed[{src}]).split({sep})[{idx}].strip()')
    elif t["type"] == "add_prefix":
        src = repr(t["source"])
        prefix = repr(t["prefix"])
        lines.append(f'    _result[{repr(ocol)}] = {prefix} + str(_parsed[{src}])')
    elif t["type"] == "add_suffix":
        src = repr(t["source"])
        suffix = repr(t["suffix"])
        lines.append(f'    _result[{repr(ocol)}] = str(_parsed[{src}]) + {suffix}')
    elif t["type"] == "combine":
        src1 = repr(t["source1"])
        src2 = repr(t["source2"])
        sep = repr(t["separator"])
        lines.append(f'    _result[{repr(ocol)}] = str(_parsed[{src1}]) + {sep} + str(_parsed[{src2}])')
    else:
        lines.append(f'    _result[{repr(ocol)}] = None')
    return lines


# ── JavaScript Code Generation ─────────────────────────────────────────

def generate_javascript_module(
    module_name: str,
    ext: str,
    filter_condition: str | None,
    transforms: dict,
    stateful_transforms: dict,
    neighbor_filter: dict | None,
) -> str:
    """Generate the JavaScript module source code."""
    output_cols = transforms["output_columns"]
    col_transforms = transforms["column_transforms"]

    lines = []
    lines.append('"use strict";')
    lines.append('')
    lines.append('const fs = require("fs");')
    lines.append('const path = require("path");')
    lines.append('const crypto = require("crypto");')
    lines.append('')
    lines.append('')

    # Parse value function
    lines.append('function _parseValue(v) {')
    lines.append('  if (v === null || v === undefined) return null;')
    lines.append('  if (typeof v === "boolean") return v;')
    lines.append('  if (typeof v === "number") return v;')
    lines.append('  const s = String(v);')
    lines.append('  const n = Number(s);')
    lines.append('  if (!isNaN(n) && s.trim() !== "") return n;')
    lines.append('  if (s.toLowerCase() === "true") return true;')
    lines.append('  if (s.toLowerCase() === "false") return false;')
    lines.append('  if (s.toLowerCase() === "null" || s === "") return null;')
    lines.append('  return s;')
    lines.append('}')
    lines.append('')
    lines.append('')

    # Try float function
    lines.append('function _tryFloat(v) {')
    lines.append('  if (v === null || v === undefined) return null;')
    lines.append('  if (typeof v === "boolean") return null;')
    lines.append('  if (typeof v === "number") return v;')
    lines.append('  const n = Number(v);')
    lines.append('  return isNaN(n) ? null : n;')
    lines.append('}')
    lines.append('')
    lines.append('')

    # State class
    lines.append('class _StreamState {')
    lines.append('  constructor() {')
    lines.append('    this.prefixSums = {};')
    lines.append('    this.prefixCounts = {};')
    lines.append('    this.windows = {};')
    lines.append('    this.stateValues = {};')
    lines.append('    this.prevValues = {};')
    lines.append('    this.prevRow = null;')
    lines.append('    this.pendingRow = null;')
    lines.append('    this.rowIndex = 0;')
    lines.append('  }')
    lines.append('')
    lines.append('  toObject() {')
    lines.append('    return {')
    lines.append('      prefixSums: { ...this.prefixSums },')
    lines.append('      prefixCounts: { ...this.prefixCounts },')
    lines.append('      windows: Object.fromEntries(')
    lines.append('        Object.entries(this.windows).map(([k, v]) => [k, [...v]])')
    lines.append('      ),')
    lines.append('      stateValues: { ...this.stateValues },')
    lines.append('      prevValues: { ...this.prevValues },')
    lines.append('      rowIndex: this.rowIndex,')
    lines.append('    };')
    lines.append('  }')
    lines.append('')
    lines.append('  static fromObject(d) {')
    lines.append('    const s = new _StreamState();')
    lines.append('    s.prefixSums = d.prefixSums || {};')
    lines.append('    s.prefixCounts = d.prefixCounts || {};')
    lines.append('    s.windows = Object.fromEntries(')
    lines.append('      Object.entries(d.windows || {}).map(([k, v]) => [k, v])')
    lines.append('    );')
    lines.append('    s.stateValues = d.stateValues || {};')
    lines.append('    s.prevValues = d.prevValues || {};')
    lines.append('    s.rowIndex = d.rowIndex || 0;')
    lines.append('    return s;')
    lines.append('  }')
    lines.append('}')
    lines.append('')
    lines.append('')

    # State configuration
    lines.extend(_gen_js_state_init(stateful_transforms, neighbor_filter))
    lines.append('')

    # Transform function
    lines.append('function _transformRow(_parsed, _state) {')
    lines.append('  const _result = {};')

    # State updates
    lines.extend(_gen_js_state_update(stateful_transforms))

    # Column transforms
    for ocol in output_cols:
        t = col_transforms[ocol]
        if t.get("type") == "stateful":
            lines.extend(_gen_js_stateful_output(ocol, stateful_transforms))
        else:
            lines.extend(_gen_js_transform(ocol, t))

    lines.append('  return _result;')
    lines.append('}')
    lines.append('')
    lines.append('')

    # Filter function
    if filter_condition is not None:
        js_cond = _python_condition_to_js(filter_condition)
        lines.append('function _keepRow(_parsed) {')
        lines.append(f'  return {js_cond};')
        lines.append('}')
    else:
        lines.append('function _keepRow(_parsed) {')
        lines.append('  return true;')
        lines.append('}')
    lines.append('')
    lines.append('')

    # Neighbor filter function
    lines.extend(_gen_js_neighbor_filter_function(neighbor_filter))
    lines.append('')

    # File extension
    lines.append(f'const _FILE_EXT = {json.dumps(ext)};')
    lines.append('')
    lines.append('')

    # Reader functions
    lines.append('function* _readRows(filePath, ext) {')
    lines.append('  const content = fs.readFileSync(filePath, "utf-8");')
    lines.append('  if (ext === "csv" || ext === "tsv") {')
    lines.append('    const lines = content.split(/\\r?\\n/).filter(l => l.trim() !== "");')
    lines.append('    if (lines.length === 0) return;')
    lines.append('    const sep = ext === "tsv" ? "\\t" : ",";')
    lines.append('    const headers = _parseCsvLine(lines[0], sep);')
    lines.append('    for (let i = 1; i < lines.length; i++) {')
    lines.append('      const vals = _parseCsvLine(lines[i], sep);')
    lines.append('      const row = {};')
    lines.append('      for (let j = 0; j < headers.length; j++) {')
    lines.append('        row[headers[j]] = j < vals.length ? vals[j] : null;')
    lines.append('      }')
    lines.append('      yield row;')
    lines.append('    }')
    lines.append('  } else if (ext === "jsonl") {')
    lines.append('    const lines = content.split(/\\r?\\n/).filter(l => l.trim() !== "");')
    lines.append('    for (const line of lines) {')
    lines.append('      yield JSON.parse(line);')
    lines.append('    }')
    lines.append('  } else if (ext === "json") {')
    lines.append('    const data = JSON.parse(content);')
    lines.append('    for (const row of data) {')
    lines.append('      yield row;')
    lines.append('    }')
    lines.append('  }')
    lines.append('}')
    lines.append('')
    lines.append('')

    # CSV line parser
    lines.append('function _parseCsvLine(line, sep) {')
    lines.append('  const result = [];')
    lines.append('  let current = "";')
    lines.append('  let inQuotes = false;')
    lines.append('  for (let i = 0; i < line.length; i++) {')
    lines.append('    const ch = line[i];')
    lines.append('    if (inQuotes) {')
    lines.append('      if (ch === \'"\') {')
    lines.append('        if (i + 1 < line.length && line[i + 1] === \'"\') {')
    lines.append('          current += \'"\';')
    lines.append('          i++;')
    lines.append('        } else {')
    lines.append('          inQuotes = false;')
    lines.append('        }')
    lines.append('      } else {')
    lines.append('        current += ch;')
    lines.append('      }')
    lines.append('    } else {')
    lines.append('      if (ch === \'"\') {')
    lines.append('        inQuotes = true;')
    lines.append('      } else if (line.substring(i, i + sep.length) === sep) {')
    lines.append('        result.push(current);')
    lines.append('        current = "";')
    lines.append('        i += sep.length - 1;')
    lines.append('      } else {')
    lines.append('        current += ch;')
    lines.append('      }')
    lines.append('    }')
    lines.append('  }')
    lines.append('  result.push(current);')
    lines.append('  return result;')
    lines.append('}')
    lines.append('')
    lines.append('')

    # DynamicPreprocessor function
    lines.append('function DynamicPreprocessor({ buffer, cache_dir = null }) {')
    lines.append('  return function(filePath) {')
    lines.append('    return {')
    lines.append('      [Symbol.iterator]() {')
    lines.append('        return _iterate(filePath, buffer, cache_dir);')
    lines.append('      }')
    lines.append('    };')
    lines.append('  };')
    lines.append('}')
    lines.append('')
    lines.append('')

    # Iterator generator
    lines.append('function* _iterate(filePath, buffer, cache_dir) {')
    lines.append('  const ext = _FILE_EXT;')
    lines.append('  const cacheKey = crypto.createHash("sha256").update(path.resolve(filePath), "utf8").digest("hex");')
    lines.append('  let cacheFile = null;')
    lines.append('  let stateFile = null;')
    lines.append('  let resumeFrom = 0;')
    lines.append('  const _state = new _StreamState();')
    lines.append('')
    lines.append('  if (cache_dir) {')
    lines.append('    fs.mkdirSync(cache_dir, { recursive: true });')
    lines.append('    cacheFile = path.join(cache_dir, "cache_" + cacheKey);')
    lines.append('    stateFile = cacheFile + ".state";')
    lines.append('    const indexFile = cacheFile + ".idx";')
    lines.append('    if (fs.existsSync(indexFile)) {')
    lines.append('      resumeFrom = parseInt(fs.readFileSync(indexFile, "utf-8").trim(), 10);')
    lines.append('    }')
    lines.append('    if (fs.existsSync(stateFile)) {')
    lines.append('      try {')
    lines.append('        const stateData = JSON.parse(fs.readFileSync(stateFile, "utf-8"));')
    lines.append('        Object.assign(_state, _StreamState.fromObject(stateData));')
    lines.append('      } catch (e) {}')
    lines.append('    }')
    lines.append('  }')
    lines.append('')
    lines.append('  let rowCount = 0;')
    lines.append('  let bufferCount = 0;')
    lines.append('')
    lines.append('  for (const rawRow of _readRows(filePath, ext)) {')
    lines.append('    const _parsed = {};')
    lines.append('    for (const _k of Object.keys(rawRow)) {')
    lines.append('      _parsed[_k] = _parseValue(rawRow[_k]);')
    lines.append('    }')
    lines.append('')
    lines.append('    if (!_keepRow(_parsed)) {')
    lines.append('      _state.rowIndex++;')
    lines.append('      continue;')
    lines.append('    }')
    lines.append('')
    lines.append('    if (!_neighborKeepRow(_parsed, _state)) {')
    lines.append('      _state.rowIndex++;')
    lines.append('      continue;')
    lines.append('    }')
    lines.append('')
    lines.append('    const _result = _transformRow(_parsed, _state);')
    lines.append('    rowCount++;')
    lines.append('    bufferCount++;')
    lines.append('    _state.rowIndex++;')
    lines.append('')
    lines.append('    if (rowCount <= resumeFrom) continue;')
    lines.append('')
    lines.append('    if (cache_dir && cacheFile) {')
    lines.append('      fs.writeFileSync(stateFile, JSON.stringify(_state.toObject()));')
    lines.append('      fs.writeFileSync(cacheFile + ".idx", String(rowCount));')
    lines.append('    }')
    lines.append('')
    lines.append('    yield _result;')
    lines.append('')
    lines.append('    if (buffer !== null && bufferCount >= buffer) {')
    lines.append('      bufferCount = 0;')
    lines.append('    }')
    lines.append('  }')
    lines.append('}')
    lines.append('')
    lines.append('')

    lines.append('module.exports = { DynamicPreprocessor };')
    lines.append('')

    return '\n'.join(lines)


def _gen_js_state_init(stateful_transforms: dict, neighbor_filter: dict | None) -> list[str]:
    """Generate JS state initialization constants."""
    lines = []
    lines.append('// Stateful transform configuration')

    for t in stateful_transforms.get("prefix_transforms", []):
        ocol = t["output_col"]
        source = t["source"]
        t_type = t["type"]
        a = t.get("a", 1)
        b = t.get("b", 0)
        lines.append(f'const _PREFIX_{ocol.upper()}_SOURCE = {json.dumps(source)};')
        lines.append(f'const _PREFIX_{ocol.upper()}_TYPE = {json.dumps(t_type)};')
        lines.append(f'const _PREFIX_{ocol.upper()}_A = {json.dumps(a)};')
        lines.append(f'const _PREFIX_{ocol.upper()}_B = {json.dumps(b)};')

    for t in stateful_transforms.get("window_transforms", []):
        ocol = t["output_col"]
        source = t["source"]
        t_type = t["type"]
        w = t["window_size"]
        a = t.get("a", 1)
        b = t.get("b", 0)
        lines.append(f'const _WINDOW_{ocol.upper()}_SOURCE = {json.dumps(source)};')
        lines.append(f'const _WINDOW_{ocol.upper()}_TYPE = {json.dumps(t_type)};')
        lines.append(f'const _WINDOW_{ocol.upper()}_SIZE = {w};')
        lines.append(f'const _WINDOW_{ocol.upper()}_A = {json.dumps(a)};')
        lines.append(f'const _WINDOW_{ocol.upper()}_B = {json.dumps(b)};')

    for t in stateful_transforms.get("state_transforms", []):
        ocol = t["output_col"]
        source = t.get("source", "")
        t_type = t["type"]
        threshold = t.get("threshold")
        initial = t.get("initial_state", 1)
        a = t.get("a", 1)
        b = t.get("b", 0)
        lines.append(f'const _STATE_{ocol.upper()}_SOURCE = {json.dumps(source)};')
        lines.append(f'const _STATE_{ocol.upper()}_TYPE = {json.dumps(t_type)};')
        if threshold is not None:
            lines.append(f'const _STATE_{ocol.upper()}_THRESHOLD = {json.dumps(threshold)};')
        lines.append(f'const _STATE_{ocol.upper()}_INITIAL = {initial};')
        lines.append(f'const _STATE_{ocol.upper()}_A = {json.dumps(a)};')
        lines.append(f'const _STATE_{ocol.upper()}_B = {json.dumps(b)};')

    if neighbor_filter:
        nf_type = neighbor_filter["type"]
        nf_col = neighbor_filter.get("column", "")
        nf_val = neighbor_filter.get("value")
        lines.append(f'const _NEIGHBOR_FILTER_TYPE = {json.dumps(nf_type)};')
        lines.append(f'const _NEIGHBOR_FILTER_COL = {json.dumps(nf_col)};')
        lines.append(f'const _NEIGHBOR_FILTER_VAL = {_js_val_repr(nf_val)};')
    else:
        lines.append('const _NEIGHBOR_FILTER_TYPE = null;')

    return lines


def _gen_js_state_update(stateful_transforms: dict) -> list[str]:
    """Generate JS state update code."""
    lines = []

    for t in stateful_transforms.get("prefix_transforms", []):
        ocol = t["output_col"]
        source = t["source"]
        lines.append(f'  // Update prefix sum for {ocol}')
        lines.append(f'  const _val_{ocol} = _tryFloat(_parsed[{json.dumps(source)}]) || 0.0;')
        lines.append(f'  if (!_state.prefixSums[{json.dumps(ocol)}]) _state.prefixSums[{json.dumps(ocol)}] = 0.0;')
        lines.append(f'  _state.prefixSums[{json.dumps(ocol)}] += _val_{ocol};')

    for t in stateful_transforms.get("window_transforms", []):
        ocol = t["output_col"]
        source = t["source"]
        w = t["window_size"]
        lines.append(f'  // Update sliding window for {ocol}')
        lines.append(f'  const _winVal_{ocol} = _tryFloat(_parsed[{json.dumps(source)}]) || 0.0;')
        lines.append(f'  if (!_state.windows[{json.dumps(ocol)}]) _state.windows[{json.dumps(ocol)}] = [];')
        lines.append(f'  _state.windows[{json.dumps(ocol)}].push(_winVal_{ocol});')
        lines.append(f'  if (_state.windows[{json.dumps(ocol)}].length > {w}) _state.windows[{json.dumps(ocol)}].shift();')

    for t in stateful_transforms.get("state_transforms", []):
        ocol = t["output_col"]
        source = t["source"]
        t_type = t["type"]
        initial = t.get("initial_state", 1)

        if t_type == "threshold_state":
            threshold = t["threshold"]
            lines.append(f'  // Update state machine for {ocol}')
            lines.append(f'  const _threshVal_{ocol} = _tryFloat(_parsed[{json.dumps(source)}]);')
            lines.append(f'  if (_state.stateValues[{json.dumps(ocol)}] === undefined) {{')
            lines.append(f'    _state.stateValues[{json.dumps(ocol)}] = {initial};')
            lines.append(f'    _state.prevValues[{json.dumps(ocol)}] = _threshVal_{ocol} !== null && _threshVal_{ocol} >= {threshold};')
            lines.append(f'  }} else {{')
            lines.append(f'    const _currAbove_{ocol} = _threshVal_{ocol} !== null && _threshVal_{ocol} >= {threshold};')
            lines.append(f'    if (_state.prevValues[{json.dumps(ocol)}] !== _currAbove_{ocol}) {{')
            lines.append(f'      _state.stateValues[{json.dumps(ocol)}]++;')
            lines.append(f'    }}')
            lines.append(f'    _state.prevValues[{json.dumps(ocol)}] = _currAbove_{ocol};')
            lines.append(f'  }}')

        elif t_type == "value_change_state":
            lines.append(f'  // Update state machine for {ocol}')
            lines.append(f'  const _changeVal_{ocol} = _parsed[{json.dumps(source)}];')
            lines.append(f'  if (_state.stateValues[{json.dumps(ocol)}] === undefined) {{')
            lines.append(f'    _state.stateValues[{json.dumps(ocol)}] = {initial};')
            lines.append(f'  }} else {{')
            lines.append(f'    if (_state.prevValues[{json.dumps(ocol)}] !== undefined && _changeVal_{ocol} !== _state.prevValues[{json.dumps(ocol)}]) {{')
            lines.append(f'      _state.stateValues[{json.dumps(ocol)}]++;')
            lines.append(f'    }}')
            lines.append(f'  }}')
            lines.append(f'  _state.prevValues[{json.dumps(ocol)}] = _changeVal_{ocol};')

    return lines


def _gen_js_stateful_output(ocol: str, stateful_transforms: dict) -> list[str]:
    """Generate JS output code for a stateful transform column."""
    lines = []

    for t in stateful_transforms.get("prefix_transforms", []):
        if t["output_col"] == ocol:
            t_type = t["type"]
            a = t.get("a", 1)
            b = t.get("b", 0)

            if t_type == "prefix_sum":
                lines.append(f'  _result[{json.dumps(ocol)}] = _state.prefixSums[{json.dumps(ocol)}] * {a} + {b};')
            elif t_type == "prefix_count":
                lines.append(f'  const _prefixCount_{ocol} = _state.rowIndex + 1;')
                lines.append(f'  _result[{json.dumps(ocol)}] = _prefixCount_{ocol} * {a} + {b};')
            return lines

    for t in stateful_transforms.get("window_transforms", []):
        if t["output_col"] == ocol:
            t_type = t["type"]
            a = t.get("a", 1)
            b = t.get("b", 0)

            if t_type == "window_sum":
                lines.append(f'  const _winSum_{ocol} = (_state.windows[{json.dumps(ocol)}] || []).reduce((a, b) => a + b, 0);')
                lines.append(f'  _result[{json.dumps(ocol)}] = _winSum_{ocol} * {a} + {b};')
            elif t_type == "window_mean":
                lines.append(f'  const _win_{ocol} = _state.windows[{json.dumps(ocol)}] || [];')
                lines.append(f'  const _winMean_{ocol} = _win_{ocol}.length > 0 ? _win_{ocol}.reduce((a, b) => a + b, 0) / _win_{ocol}.length : 0.0;')
                lines.append(f'  _result[{json.dumps(ocol)}] = _winMean_{ocol} * {a} + {b};')
            elif t_type == "window_count":
                lines.append(f'  const _winCount_{ocol} = (_state.windows[{json.dumps(ocol)}] || []).length;')
                lines.append(f'  _result[{json.dumps(ocol)}] = _winCount_{ocol} * {a} + {b};')
            return lines

    for t in stateful_transforms.get("state_transforms", []):
        if t["output_col"] == ocol:
            a = t.get("a", 1)
            b = t.get("b", 0)
            lines.append(f'  _result[{json.dumps(ocol)}] = (_state.stateValues[{json.dumps(ocol)}] || 1) * {a} + {b};')
            return lines

    lines.append(f'  _result[{json.dumps(ocol)}] = null;')
    return lines


def _gen_js_neighbor_filter_function(neighbor_filter: dict | None) -> list[str]:
    """Generate JS neighbor-based filter function."""
    lines = []
    lines.append('function _neighborKeepRow(_parsed, _state) {')

    if neighbor_filter is None:
        lines.append('  return true;')
        lines.append('}')
        return lines

    nf_type = neighbor_filter["type"]

    if nf_type == "drop_if_next_equals":
        col = neighbor_filter["column"]
        val = neighbor_filter["value"]
        lines.append('  // Lookahead filter')
        lines.append(f'  if (_state.prevRow && _parsed[{json.dumps(col)}] === {_js_val_repr(val)}) {{')
        lines.append('    _state.prevRow = _parsed;')
        lines.append('    return false;')
        lines.append('  }')
        lines.append('  _state.prevRow = _parsed;')
        lines.append('  return true;')

    elif nf_type == "drop_if_prev_equals":
        col = neighbor_filter["column"]
        val = neighbor_filter["value"]
        lines.append(f'  if (_state.prevRow && _state.prevRow[{json.dumps(col)}] === {_js_val_repr(val)}) {{')
        lines.append('    _state.prevRow = _parsed;')
        lines.append('    return false;')
        lines.append('  }')
        lines.append('  _state.prevRow = _parsed;')
        lines.append('  return true;')

    elif nf_type == "keep_first_in_run":
        col = neighbor_filter["column"]
        lines.append(f'  const _currVal = _parsed[{json.dumps(col)}];')
        lines.append(f'  if (_state.prevValues[{json.dumps(col)}] !== undefined && _currVal === _state.prevValues[{json.dumps(col)}]) {{')
        lines.append(f'    _state.prevValues[{json.dumps(col)}] = _currVal;')
        lines.append('    return false;')
        lines.append('  }')
        lines.append(f'  _state.prevValues[{json.dumps(col)}] = _currVal;')
        lines.append('  return true;')

    elif nf_type == "keep_last_in_run":
        lines.append('  return true;')

    else:
        lines.append('  return true;')

    lines.append('}')
    return lines


def _gen_js_transform(ocol: str, t: dict) -> list[str]:
    """Generate JavaScript lines for a single column transform."""
    lines = []
    ocol_js = json.dumps(ocol)

    if t["type"] == "identity":
        lines.append(f'  _result[{ocol_js}] = _parsed[{json.dumps(t["source"])}];')
    elif t["type"] == "rename":
        lines.append(f'  _result[{ocol_js}] = _parsed[{json.dumps(t["source"])}];')
    elif t["type"] == "constant":
        lines.append(f'  _result[{ocol_js}] = {_js_val_repr(t["value"])};')
    elif t["type"] == "linear":
        src = json.dumps(t["source"])
        a = t["a"]
        b = t["b"]
        if a == 1 and b == 0:
            lines.append(f'  _result[{ocol_js}] = _parsed[{src}];')
        elif a == 1:
            lines.append(f'  _result[{ocol_js}] = _parsed[{src}] + {_js_val_repr(b)};')
        elif a == 0:
            lines.append(f'  _result[{ocol_js}] = {_js_val_repr(b)};')
        elif b == 0:
            lines.append(f'  _result[{ocol_js}] = _parsed[{src}] * {_js_val_repr_raw(a)};')
        else:
            lines.append(f'  _result[{ocol_js}] = _parsed[{src}] * {_js_val_repr_raw(a)} + {_js_val_repr(b)};')
    elif t["type"] == "string_lower":
        lines.append(f'  _result[{ocol_js}] = String(_parsed[{json.dumps(t["source"])}]).toLowerCase();')
    elif t["type"] == "string_upper":
        lines.append(f'  _result[{ocol_js}] = String(_parsed[{json.dumps(t["source"])}]).toUpperCase();')
    elif t["type"] == "string_trim":
        lines.append(f'  _result[{ocol_js}] = String(_parsed[{json.dumps(t["source"])}]).trim();')
    elif t["type"] == "string_trim_lower":
        lines.append(f'  _result[{ocol_js}] = String(_parsed[{json.dumps(t["source"])}]).trim().toLowerCase();')
    elif t["type"] == "string_trim_upper":
        lines.append(f'  _result[{ocol_js}] = String(_parsed[{json.dumps(t["source"])}]).trim().toUpperCase();')
    elif t["type"] == "split":
        src = json.dumps(t["source"])
        sep = json.dumps(t["separator"])
        idx = t["part_index"]
        lines.append(f'  _result[{ocol_js}] = String(_parsed[{src}]).split({sep})[{idx}].trim();')
    elif t["type"] == "add_prefix":
        src = json.dumps(t["source"])
        prefix = json.dumps(t["prefix"])
        lines.append(f'  _result[{ocol_js}] = {prefix} + String(_parsed[{src}]);')
    elif t["type"] == "add_suffix":
        src = json.dumps(t["source"])
        suffix = json.dumps(t["suffix"])
        lines.append(f'  _result[{ocol_js}] = String(_parsed[{src}]) + {suffix};')
    elif t["type"] == "combine":
        src1 = json.dumps(t["source1"])
        src2 = json.dumps(t["source2"])
        sep = json.dumps(t["separator"])
        lines.append(f'  _result[{ocol_js}] = String(_parsed[{src1}]) + {sep} + String(_parsed[{src2}]);')
    else:
        lines.append(f'  _result[{ocol_js}] = null;')
    return lines


def _js_val_repr(v: Any) -> str:
    """Return JS literal representation of a value."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if v == int(v) and not math.isinf(v):
            return str(int(v))
        return str(v)
    return json.dumps(str(v))


def _js_val_repr_raw(v: Any) -> str:
    """Return JS literal representation keeping float as float."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(v)
    return json.dumps(str(v))


def _python_condition_to_js(cond: str) -> str:
    """Convert a Python filter condition to JavaScript."""
    js = cond
    # Replace Python keywords
    js = js.replace(" and ", " && ")
    js = js.replace(" or ", " || ")
    js = js.replace("True", "true")
    js = js.replace("False", "false")
    js = js.replace("None", "null")
    js = js.replace("!=", " !== ")
    js = js.replace("==", " === ")

    js = js.replace("_try_float(", "_tryFloat(")
    js = re.sub(r"(?<=\=\=\s)'([^']*)'", r'"\1"', js)
    js = re.sub(r"(?<=\!\=\s)'([^']*)'", r'"\1"', js)
    js = re.sub(r"'([^']*)'(?=\s*\=\=)", r'"\1"', js)
    js = re.sub(r"'([^']*)'(?=\s*\!\=)", r'"\1"', js)

    return js


# ── Main ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Dynamic buffer code generator")
    parser.add_argument("module_name", help="Name of the generated module")
    parser.add_argument("--output", required=True, help="Output directory")
    parser.add_argument("--sample", required=True, help="Sample directory with input/output files")
    lang_group = parser.add_mutually_exclusive_group(required=True)
    lang_group.add_argument("--python", action="store_true", help="Generate Python module")
    lang_group.add_argument("--javascript", action="store_true", help="Generate JavaScript module")

    args = parser.parse_args()

    # Detect file extension and load samples
    ext = detect_extension(args.sample)
    input_path = os.path.join(args.sample, f"input.{ext}")
    output_path = os.path.join(args.sample, f"output.{ext}")
    input_rows = load_rows(input_path, ext)
    output_rows = load_rows(output_path, ext)

    # Infer transformations
    filter_condition, kept_indices = infer_filter_and_match(input_rows, output_rows)

    # Try to infer neighbor-based filtering if per-row filter doesn't explain all
    neighbor_filter = None
    if len(kept_indices) < len(input_rows) and filter_condition is None:
        neighbor_filter = infer_neighbor_filter(input_rows, output_rows)

    transforms = infer_transformations(input_rows, output_rows, kept_indices)

    # Infer stateful transforms (only for columns not explained by simple transforms)
    stateful_transforms = infer_stateful_transforms(
        input_rows, output_rows, kept_indices, transforms["column_transforms"]
    )

    # Update column transforms to mark stateful columns
    for t in stateful_transforms.get("prefix_transforms", []):
        ocol = t["output_col"]
        if ocol in transforms["column_transforms"]:
            transforms["column_transforms"][ocol] = {"type": "stateful", "output_col": ocol}
    for t in stateful_transforms.get("window_transforms", []):
        ocol = t["output_col"]
        if ocol in transforms["column_transforms"]:
            transforms["column_transforms"][ocol] = {"type": "stateful", "output_col": ocol}
    for t in stateful_transforms.get("state_transforms", []):
        ocol = t["output_col"]
        if ocol in transforms["column_transforms"]:
            transforms["column_transforms"][ocol] = {"type": "stateful", "output_col": ocol}

    # Generate output
    os.makedirs(args.output, exist_ok=True)

    if args.python:
        module_dir = os.path.join(args.output, args.module_name)
        os.makedirs(module_dir, exist_ok=True)

        code = generate_python_module(
            args.module_name, ext, filter_condition, transforms,
            stateful_transforms, neighbor_filter
        )
        module_file = os.path.join(module_dir, "preprocessor.py")
        with open(module_file, "w", encoding="utf-8") as f:
            f.write(code)

        # Write __init__.py
        init_file = os.path.join(module_dir, "__init__.py")
        init_code = f"from .preprocessor import DynamicPreprocessor\n"
        with open(init_file, "w", encoding="utf-8") as f:
            f.write(init_code)

    elif args.javascript:
        module_dir = os.path.join(args.output, args.module_name)
        os.makedirs(module_dir, exist_ok=True)

        code = generate_javascript_module(
            args.module_name, ext, filter_condition, transforms,
            stateful_transforms, neighbor_filter
        )
        module_file = os.path.join(module_dir, "index.js")
        with open(module_file, "w", encoding="utf-8") as f:
            f.write(code)


if __name__ == "__main__":
    main()
