#!/usr/bin/env python3
"""
Dynamic buffer code generator.

Given a module name, a sample directory with input/output files,
a target language, and an output directory, this script:
1. Infers a deterministic transformation from input to output.
2. Generates a module implementing a DynamicPreprocessor that applies
   the inferred transformation with streaming support.
"""

import argparse
import csv
import json
import os
import sys
import math
import hashlib
from typing import Any

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
    # Try int
    try:
        return int(s)
    except (ValueError, TypeError):
        pass
    # Try float
    try:
        return float(s)
    except (ValueError, TypeError):
        pass
    # Boolean strings
    if s.lower() == "true":
        return True
    if s.lower() == "false":
        return False
    # Null strings
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


# ── Transformation Inference ────────────────────────────────────────────

def infer_filter_condition(
    input_rows: list[dict], output_rows: list[dict]
) -> str | None:
    """
    Infer a filtering condition that explains which input rows survive
    into the output. Returns a Python expression string (or None if no filtering).
    """
    if len(input_rows) == len(output_rows):
        return None

    # Build mapping from output rows to input rows by position.
    # We need to figure out which input rows map to which output rows.
    # Strategy: since order is preserved, greedy match.
    kept_indices: list[int] = _match_rows(input_rows, output_rows)

    if len(kept_indices) == len(input_rows):
        return None  # all kept, no filter

    kept = [input_rows[i] for i in kept_indices]
    dropped = [input_rows[i] for i in range(len(input_rows)) if i not in set(kept_indices)]

    if not dropped:
        return None

    # Try to infer a filter condition
    condition = _infer_predicate(kept, dropped, input_rows[0].keys())
    return condition


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
    # Normalize all values
    kept_n = [{k: parse_value(v) for k, v in r.items()} for r in kept]
    dropped_n = [{k: parse_value(v) for k, v in r.items()} for r in dropped]

    # Collect candidate simple conditions
    conditions: list[tuple[str, str]] = []  # (column, python_expr)

    for col in all_columns:
        kept_vals = [r.get(col) for r in kept_n]
        dropped_vals = [r.get(col) for r in dropped_n]

        # Try equality to a constant
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

        # For each value that appears in kept but not dropped,
        # or appears in dropped but not kept
        for val in unique_kept:
            if val not in unique_dropped and len(unique_kept) == 1:
                # All kept rows have this value for this column
                cond = _make_eq_expr(col, val)
                if _check_condition(cond, kept_n, dropped_n, all_columns):
                    conditions.append((col, cond))

        for val in unique_dropped:
            if val not in unique_kept and len(unique_dropped) == 1:
                # All dropped rows have this value for this column
                cond = _make_neq_expr(col, val)
                if _check_condition(cond, kept_n, dropped_n, all_columns):
                    conditions.append((col, cond))

        # Try comparison conditions
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
                # Check >= threshold
                if min_d < min_k:
                    threshold = min_k
                    if all(d < threshold for d in dn) and all(k >= threshold for k in kn):
                        cond = _make_gte_expr(col, threshold)
                        if _check_condition(cond, kept_n, dropped_n, all_columns):
                            conditions.append((col, cond))
                # Check > threshold
                if min_d <= min_k:
                    threshold = min_d
                    if all(d <= threshold for d in dn) and all(k > threshold for k in kn):
                        cond = _make_gt_expr(col, threshold)
                        if _check_condition(cond, kept_n, dropped_n, all_columns):
                            conditions.append((col, cond))
                # Check <= threshold
                if max_d > max_k:
                    threshold = max_k
                    if all(d > threshold for d in dn) and all(k <= threshold for k in kn):
                        cond = _make_lte_expr(col, threshold)
                        if _check_condition(cond, kept_n, dropped_n, all_columns):
                            conditions.append((col, cond))
                # Check < threshold
                if max_d >= max_k:
                    threshold = max_d
                    if all(d >= threshold for d in dn) and all(k < threshold for k in kn):
                        cond = _make_lt_expr(col, threshold)
                        if _check_condition(cond, kept_n, dropped_n, all_columns):
                            conditions.append((col, cond))

    if conditions:
        return conditions[0][1]

    # Try AND combinations of two conditions
    simple_conds = _get_all_simple_conditions(kept_n, dropped_n, all_columns)
    for c1 in simple_conds:
        for c2 in simple_conds:
            if c1 >= c2:
                continue
            combined = f"({c1} and {c2})"
            if _check_condition(combined, kept_n, dropped_n, all_columns):
                return combined

    # Try OR combinations
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

        # Equality
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

        # Numeric comparisons
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
    # Build a safe evaluation environment
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
    ns = {}
    for col in all_columns:
        val = row.get(col)
        ns[col] = val
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

    # Fallback: identity (should not happen with valid samples)
    if ocol in input_cols:
        return {"type": "identity", "source": ocol}

    return {"type": "constant", "value": None}


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
        # Check if y1 == y2 (constant)
        if abs(y2 - y1) < 1e-12:
            # All same -> constant
            return None  # handled by constant check
        return None

    a = (y2 - y1) / (x2 - x1)
    b = y1 - a * x1

    # Verify against all pairs
    for x, y in pairs:
        expected = a * x + b
        if abs(expected - y) > 1e-6:
            return None

    # Check if a is effectively 1 and b is effectively 0 (identity)
    if abs(a - 1.0) < 1e-9 and abs(b) < 1e-9:
        return {"type": "identity", "source": icol} if icol == ocol else {"type": "rename", "source": icol}

    # Check if a is effectively 0 (constant)
    if abs(a) < 1e-9:
        return {"type": "constant", "value": b if b != int(b) else int(b)}

    # Clean up a and b
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
    lines.append('')
    lines.append('')

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

    # Generate transform function
    lines.append('def _transform_row(_parsed):')
    lines.append('    """Apply column transformations to a parsed row."""')
    lines.append('    _result = {}')

    for ocol in output_cols:
        t = col_transforms[ocol]
        lines.append(f'    # Transform for {repr(ocol)}')
        lines.extend(_gen_python_transform(ocol, t))

    lines.append('    return _result')
    lines.append('')
    lines.append('')

    # Generate filter function
    if filter_condition is not None:
        safe_cond = filter_condition
        lines.append('def _keep_row(_parsed):')
        lines.append('    """Determine if a row should be kept."""')
        lines.append(f'    return {safe_cond}')
        lines.append('')
        lines.append('')
    else:
        lines.append('def _keep_row(_parsed):')
        lines.append('    return True')
        lines.append('')
        lines.append('')

    # Output columns as a list
    lines.append(f'_OUTPUT_COLUMNS = {repr(output_cols)}')
    lines.append('')

    # File extension
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
    lines.append('    resume_from = 0')
    lines.append('    if cache_dir is not None:')
    lines.append('        os.makedirs(cache_dir, exist_ok=True)')
    lines.append('        cache_file = os.path.join(cache_dir, f"cache_{cache_key}")')
    lines.append('        index_file = cache_file + ".idx"')
    lines.append('        if os.path.isfile(index_file):')
    lines.append('            with open(index_file, "r") as f:')
    lines.append('                resume_from = int(f.read().strip())')
    lines.append('    row_count = 0')
    lines.append('    buf = []')
    lines.append('    try:')
    lines.append('        for raw_row in _read_rows(path, ext):')
    lines.append('            _parsed = {}')
    lines.append('            for _k, _v in raw_row.items():')
    lines.append('                _parsed[_k] = _parse_value(_v)')
    lines.append('            if not _keep_row(_parsed):')
    lines.append('                continue')
    lines.append('            _result = _transform_row(_parsed)')
    lines.append('            row_count += 1')
    lines.append('            if row_count <= resume_from:')
    lines.append('                continue')
    lines.append('            if cache_dir is not None and cache_file is not None:')
    lines.append('                with open(cache_file + ".idx", "w") as f:')
    lines.append('                    f.write(str(row_count))')
    lines.append('            yield _result')
    lines.append('    finally:')
    lines.append('        pass')
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


# ── JavaScript Code Generation ─────────────────────────────────────────

def generate_javascript_module(
    module_name: str,
    ext: str,
    filter_condition: str | None,
    transforms: dict,
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

    # Transform function
    lines.append('function _transformRow(_parsed) {')
    lines.append('  const _result = {};')
    for ocol in output_cols:
        t = col_transforms[ocol]
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
    lines.append('  let resumeFrom = 0;')
    lines.append('  if (cache_dir) {')
    lines.append('    fs.mkdirSync(cache_dir, { recursive: true });')
    lines.append('    cacheFile = path.join(cache_dir, "cache_" + cacheKey);')
    lines.append('    const indexFile = cacheFile + ".idx";')
    lines.append('    if (fs.existsSync(indexFile)) {')
    lines.append('      resumeFrom = parseInt(fs.readFileSync(indexFile, "utf-8").trim(), 10);')
    lines.append('    }')
    lines.append('  }')
    lines.append('  let rowCount = 0;')
    lines.append('  for (const rawRow of _readRows(filePath, ext)) {')
    lines.append('    const _parsed = {};')
    lines.append('    for (const _k of Object.keys(rawRow)) {')
    lines.append('      _parsed[_k] = _parseValue(rawRow[_k]);')
    lines.append('    }')
    lines.append('    if (!_keepRow(_parsed)) continue;')
    lines.append('    const _result = _transformRow(_parsed);')
    lines.append('    rowCount++;')
    lines.append('    if (rowCount <= resumeFrom) continue;')
    lines.append('    if (cache_dir && cacheFile) {')
    lines.append('      fs.writeFileSync(cacheFile + ".idx", String(rowCount));')
    lines.append('    }')
    lines.append('    yield _result;')
    lines.append('  }')
    lines.append('}')
    lines.append('')
    lines.append('')

    lines.append('module.exports = { DynamicPreprocessor };')
    lines.append('')

    return '\n'.join(lines)


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

    # Replace _parsed[...] with _parsed[...] (same syntax works)
    # Replace _try_float(...) with _tryFloat(...)
    js = js.replace("_try_float(", "_tryFloat(")
    # Replace repr-style strings: Python uses single quotes, JS uses double
    # Actually, the conditions use repr() which may produce single-quoted strings.
    # We need to be more careful here.
    # Since conditions are generated with repr(), strings will use single quotes.
    # Replace 'string' with "string" for JS compatibility
    # This is a simple heuristic:
    import re
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
    transforms = infer_transformations(input_rows, output_rows, kept_indices)

    # Generate output
    os.makedirs(args.output, exist_ok=True)

    if args.python:
        module_dir = os.path.join(args.output, args.module_name)
        os.makedirs(module_dir, exist_ok=True)

        code = generate_python_module(args.module_name, ext, filter_condition, transforms)
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

        code = generate_javascript_module(args.module_name, ext, filter_condition, transforms)
        module_file = os.path.join(module_dir, "index.js")
        with open(module_file, "w", encoding="utf-8") as f:
            f.write(code)


if __name__ == "__main__":
    main()
