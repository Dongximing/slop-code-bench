#!/usr/bin/env python3
"""
Dynamic Buffer Code Generator

Generates a DynamicPreprocessor module that can transform data files
based on inferred transformations from sample input/output pairs.
"""

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple, Set, Union
import numpy as np
import pandas as pd

# Data loading / saving

SUPPORTED_EXTS = {"csv", "tsv", "jsonl", "json"}


def detect_extension(directory: Path) -> str:
    for f in directory.iterdir():
        if f.is_file() and f.name.startswith(("input.", "output.")):
            return f.suffix.lstrip(".")
    raise ValueError("No supported data files found in sample directory")


def load_data(path: Path) -> pd.DataFrame:
    ext = path.suffix.lstrip(".")
    if ext == "csv":
        return pd.read_csv(path, dtype=str)
    elif ext == "tsv":
        return pd.read_csv(path, sep="\t", dtype=str)
    elif ext == "jsonl":
        records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        return pd.DataFrame(records)
    elif ext == "json":
        data = json.loads(path.read_text())
        if isinstance(data, list):
            return pd.DataFrame(data)
        else:
            raise ValueError(f"Expected JSON array in {path}, got {type(data)}")
    else:
        raise ValueError(f"Unsupported extension: {ext}")


def infer_delimiter(ext: str) -> str:
    if ext == "tsv":
        return "\t"
    return ","


# Transformation inference

@dataclass
class ColumnTransform:
    """Describes how an input column is transformed to an output column."""
    output_col: str          # name of column in output
    input_col: Optional[str] # name of input column (None if constant)
    transform_type: str      # "identity", "rename", "linear", "trim", "lower", "upper", "const", "copy", "split", "prefix", "suffix", "combine"
    a: float = 1.0           # for linear transform: output = a * input + b
    b: float = 0.0
    constant_value: Any = None
    args: tuple = ()         # extra args (e.g., delimiter for split)


@dataclass
class FilterPredicate:
    """A simple predicate used for row filtering."""
    col: str
    op: str           # "==", "!=", "<", "<=", ">", ">="
    value: Any
    is_and: bool = False
    is_or: bool = False
    children: List["FilterPredicate"] = field(default_factory=list)


@dataclass
class InferredTransformation:
    """The complete inferred transformation."""
    keep_cols: Set[str]           # columns present in output
    col_transforms: Dict[str, ColumnTransform]  # output col -> transform
    filter_predicates: List[FilterPredicate]  # row must satisfy all
    original_cols: Set[str]       # columns in input
    # Stateful transforms
    prefix_transforms: Dict[str, "PrefixTransform"] = field(default_factory=dict)
    sliding_window_transforms: Dict[str, "SlidingWindowTransform"] = field(default_factory=dict)
    state_machine_transforms: Dict[str, "StateMachineTransform"] = field(default_factory=dict)
    neighbor_filters: List["NeighborFilter"] = field(default_factory=list)


@dataclass
class PrefixTransform:
    """Cumulative/prefix transform: out[i] depends on all previous rows."""
    output_col: str          # name of output column
    input_col: str           # name of input column to accumulate
    agg_type: str            # "sum", "count", "mean"
    # For arithmetic expressions of the form: output = a * S + b
    a: float = 1.0
    b: float = 0.0


@dataclass
class SlidingWindowTransform:
    """Fixed-size sliding window transform."""
    output_col: str          # name of output column
    input_col: str           # name of input column
    agg_type: str            # "sum", "mean", "count", "min", "max"
    window_size: int         # W, 1 <= W <= 64


@dataclass
class StateTransition:
    """A single state transition rule."""
    condition_col: str       # column to check
    condition_op: str        # "==", "!=", "<", "<=", ">", ">="
    condition_value: Any     # value to compare against
    new_state: Any           # state to transition to


@dataclass
class StateMachineTransform:
    """State-machine-style sequence labeling."""
    output_col: str          # name of output column
    input_cols: List[str]    # columns that influence state transitions
    initial_state: Any       # starting state
    transitions: List[StateTransition] = field(default_factory=list)
    # For simple increments when condition met
    increment_on_col: Optional[str] = None   # if set, increment when this column changes


@dataclass
class NeighborFilter:
    """Neighbor-based filtering decision."""
    depends_on_next: bool    # True if decision uses row i+1
    depends_on_prev: bool    # True if decision uses row i-1
    condition_on_next: Optional[Tuple[str, str, Any]] = None  # (col, op, value) for next row
    condition_on_prev: Optional[Tuple[str, str, Any]] = None  # (col, op, value) for prev row
    condition_on_self: Optional[Tuple[str, str, Any]] = None  # (col, op, value) for self
    keep_if_matches: bool = True  # True means keep row if condition matches



def try_parse_number(s: str) -> Union[int, float, str]:
    """Try to convert string to int, then float, else return string."""
    if not isinstance(s, str):
        return s
    try:
        return int(s)
    except ValueError:
        try:
            f = float(s)
            # Avoid treating "30.0" as float when it could be int context
            if f == int(f):
                return int(f)
            return f
        except ValueError:
            return s


def values_match(v1, v2, op: str) -> bool:
    v1 = try_parse_number(str(v1) if not isinstance(v1, (int, float, bool, type(None))) else v1)
    v2 = try_parse_number(str(v2) if not isinstance(v2, (int, float, bool, type(None))) else v2)
    # Handle None
    if v1 is None or v2 is None:
        if op == "==":
            return v1 is None and v2 is None
        elif op == "!=":
            return not (v1 is None and v2 is None)
        return False
    # Handle booleans
    if isinstance(v1, bool) or isinstance(v2, bool):
        v1 = bool(v1)
        v2 = bool(v2)
    # Handle numeric comparison
    try:
        if isinstance(v1, (int, float)) and isinstance(v2, (int, float)):
            if op == "==":
                return v1 == v2
            elif op == "!=":
                return v1 != v2
            elif op == "<":
                return v1 < v2
            elif op == "<=":
                return v1 <= v2
            elif op == ">":
                return v1 > v2
            elif op == ">=":
                return v1 >= v2
    except:
        pass
    # String comparison fallback
    if op == "==":
        return str(v1) == str(v2)
    elif op == "!=":
        return str(v1) != str(v2)
    return False


def infer_filter_predicate(input_df: pd.DataFrame, output_df: pd.DataFrame) -> List[FilterPredicate]:
    """
    Infer what filter conditions were applied to go from input to output.
    Returns a list of AND-ed simple predicates.
    For OR conditions within the same column, we use OR child predicates.
    """
    if len(output_df) == 0 and len(input_df) > 0:
        # Everything was filtered out — can't easily infer, return empty
        return []

    if len(output_df) == len(input_df):
        return []  # No filtering

    # Add a temporary index to track original positions
    input_df = input_df.copy()
    input_df['_row_idx'] = range(len(input_df))
    output_df = output_df.copy()
    output_df['_row_idx'] = range(len(output_df))

    # Find columns present in both dataframes
    common_cols = set(input_df.columns) & set(output_df.columns)
    common_cols.discard('_row_idx')

    # Match rows by finding output rows in input dataframe
    kept_positions = []
    used_input_indices = set()

    for out_idx, out_row in output_df.iterrows():
        # Find matching input row
        for in_idx, in_row in input_df.iterrows():
            if in_idx in used_input_indices:
                continue
            # Check if rows match on common columns
            matches = True
            for col in common_cols:
                if str(in_row[col]) != str(out_row[col]):
                    matches = False
                    break
            if matches:
                kept_positions.append(in_idx)
                used_input_indices.add(in_idx)
                break

    dropped_positions = [p for p in range(len(input_df)) if p not in kept_positions]

    if not dropped_positions:
        return []  # No filtering

    # Analyze columns
    kept_df = input_df.loc[kept_positions]
    dropped_df = input_df.loc[dropped_positions]

    predicates = []
    for col in common_cols:
        col_vals_kept = set(kept_df[col].dropna().unique()) if kept_df[col].notna().any() else set()
        col_vals_dropped = set(dropped_df[col].dropna().unique()) if dropped_df[col].notna().any() else set()

        if not col_vals_kept or not col_vals_dropped:
            continue  # Column doesn't help differentiate

        only_kept = col_vals_kept - col_vals_dropped
        only_dropped = col_vals_dropped - col_vals_kept

        # Check for range conditions
        try:
            kept_numeric = pd.to_numeric(kept_df[col], errors='coerce').dropna()
            dropped_numeric = pd.to_numeric(dropped_df[col], errors='coerce').dropna()
            if len(kept_numeric) > 0 and len(dropped_numeric) > 0:
                if kept_numeric.min() > dropped_numeric.max():
                    predicates.append(FilterPredicate(col=col, op=">", value=float(dropped_numeric.max())))
                    continue
                elif kept_numeric.max() < dropped_numeric.min():
                    predicates.append(FilterPredicate(col=col, op="<", value=float(dropped_numeric.min())))
                    continue
        except:
            pass

        # For each value in kept-only set, create OR condition
        if len(only_kept) >= 1:
            if len(only_kept) <= 10:
                children = [FilterPredicate(col=col, op="==", value=v) for v in only_kept]
                if len(children) == 1:
                    predicates.append(children[0])
                else:
                    main = children[0]
                    main.children = children[1:]
                    main.is_or = True
                    predicates.append(main)
            else:
                # Many values, use complement (drop values in dropped set)
                for v in only_dropped:
                    predicates.append(FilterPredicate(col=col, op="!=", value=v))

    return predicates


def infer_column_transform(input_val: Any, output_val: Any) -> Tuple[str, ColumnTransform]:
    """
    Given an input value and output value, infer what transform was applied.
    Returns (output_col_name, ColumnTransform).
    """
    input_val = str(input_val) if not isinstance(input_val, (str, int, float, bool, type(None))) else input_val
    output_val = str(output_val) if not isinstance(output_val, (str, int, float, bool, type(None))) else output_val

    # Constant column
    if input_val == "" and output_val is not None:
        return "col", ColumnTransform(output_col="col", input_col=None, transform_type="const", constant_value=output_val)

    # Check for linear transform: try to parse as numbers
    try:
        in_num = float(input_val) if input_val != "" else None
        out_num = float(output_val) if output_val != "" else None
        if in_num is not None and out_num is not None:
            # Solve for a, b: out = a * in + b
            # Use another point to check consistency, but just compute a, b from first row
            # For now, just store identity (we'll infer across rows)
            pass
    except (ValueError, TypeError):
        pass

    # String transformations
    if input_val and output_val:
        s_in = str(input_val).strip()
        s_out = str(output_val)

        if s_in == s_out:
            return "col", ColumnTransform(output_col="col", input_col="col", transform_type="identity")

        # Trim
        if s_out == s_in.strip():
            return "col", ColumnTransform(output_col="col", input_col="col", transform_type="trim")

        # Lowercase
        if s_out == s_in.lower():
            return "col", ColumnTransform(output_col="col", input_col="col", transform_type="lower")

        # Uppercase
        if s_out == s_in.upper():
            return "col", ColumnTransform(output_col="col", input_col="col", transform_type="upper")

        # Prefix check
        for prefix_len in range(1, min(len(s_out), len(s_in)) + 1):
            if s_out.startswith(s_in[:prefix_len]):
                suffix = s_out[prefix_len:]
                if suffix and not s_out.endswith(suffix):
                    # This is a prefix transform
                    return "col", ColumnTransform(output_col="col", input_col="col", transform_type="prefix", args=(s_in[:prefix_len],))

    return "col", ColumnTransform(output_col="col", input_col="col", transform_type="identity")


def infer_transformation(input_df: pd.DataFrame, output_df: pd.DataFrame) -> InferredTransformation:
    """
    Infer the complete transformation from input to output.
    """
    input_cols = set(input_df.columns.tolist())
    output_cols = set(output_df.columns.tolist())

    # Determine dropped columns
    dropped_input_cols = input_cols - output_cols

    # Check for renamed columns
    renamed = {}  # output_name -> input_name
    unchanged = set()

    for out_col in output_cols:
        if out_col in input_cols:
            unchanged.add(out_col)
        else:
            # Check if there's a column in input that has same values (except possibly transformed)
            for in_col in input_cols:
                if in_col not in output_cols and in_col not in renamed and in_col not in dropped_input_cols:
                    # Compare values
                    in_vals = input_df[in_col].astype(str).tolist()
                    out_vals = output_df[out_col].astype(str).tolist()
                    if len(in_vals) == len(out_vals):
                        # Check if values match (with possible transform)
                        all_match = True
                        for iv, ov in zip(in_vals, out_vals):
                            if iv.strip() == ov.strip() or iv == ov:
                                continue
                            all_match = False
                            break
                        if all_match:
                            renamed[out_col] = in_col
                            break

    input_cols_used = input_cols - dropped_input_cols - set(renamed.values())

    # Build column transforms
    col_transforms: Dict[str, ColumnTransform] = {}

    for out_col in output_cols:
        if out_col in renamed:
            in_col = renamed[out_col]
            # Check if there's a simple transform
            col_transforms[out_col] = ColumnTransform(
                output_col=out_col,
                input_col=in_col,
                transform_type="rename"
            )
        elif out_col in input_cols:
            in_col = out_col
            col_transforms[out_col] = ColumnTransform(
                output_col=out_col,
                input_col=in_col,
                transform_type="identity"
            )
        else:
            # New column not in input - could be constant or computed
            # Check if all values are the same → constant
            vals = output_df[out_col].dropna().unique()
            if len(vals) == 1:
                col_transforms[out_col] = ColumnTransform(
                    output_col=out_col,
                    input_col=None,
                    transform_type="const",
                    constant_value=vals[0]
                )
            else:
                # Try to figure out what it's based on
                # Leave as copy for now - will check for stateful transforms below
                col_transforms[out_col] = ColumnTransform(
                    output_col=out_col,
                    input_col=None,
                    transform_type="copy",
                    constant_value=None
                )

    # Add transforms for renamed/dropped columns
    for in_col in dropped_input_cols:
        if in_col not in renamed.values():
            pass  # Just dropped

    # Infer filter
    filter_preds = infer_filter_predicate(input_df, output_df)

    # Infer stateful transforms - these override basic transforms for the same columns
    prefix_transforms = infer_prefix_transform(input_df, output_df)
    sliding_transforms = infer_sliding_window_transform(input_df, output_df)
    state_transforms = infer_state_machine_transform(input_df, output_df)

    # Replace copy transforms with actual stateful transforms
    for out_col in prefix_transforms:
        if out_col in col_transforms:
            del col_transforms[out_col]
    for out_col in sliding_transforms:
        if out_col in col_transforms:
            del col_transforms[out_col]
    for out_col in state_transforms:
        if out_col in col_transforms:
            del col_transforms[out_col]

    # Infer neighbor filters
    neighbor_filters = infer_neighbor_filter(input_df, output_df)

    return InferredTransformation(
        keep_cols=output_cols,
        col_transforms=col_transforms,
        filter_predicates=filter_preds,
        original_cols=input_cols,
        prefix_transforms=prefix_transforms,
        sliding_window_transforms=sliding_transforms,
        state_machine_transforms=state_transforms,
        neighbor_filters=neighbor_filters
    )


def infer_prefix_transform(input_df: pd.DataFrame, output_df: pd.DataFrame) -> Dict[str, PrefixTransform]:
    """Detect prefix/cumulative transforms."""
    transforms = {}
    input_cols = set(input_df.columns.tolist())

    for out_col in output_df.columns:
        if out_col in input_cols:
            continue

        # Check if output can be expressed as a * prefix_sum(input) + b
        # Try to find an input column that this depends on
        for in_col in input_cols:
            if in_col not in output_df.columns:
                try:
                    in_vals = pd.to_numeric(input_df[in_col], errors='coerce').fillna(0)
                    out_vals = pd.to_numeric(output_df[out_col], errors='coerce')

                    if len(in_vals) != len(out_vals):
                        continue
                    if in_vals.isna().all() or out_vals.isna().all():
                        continue

                    # Compute prefix sums
                    prefix_sum = in_vals.cumsum()

                    # Try to find a and b such that out = a * prefix_sum + b
                    # Use first two points to solve
                    valid_mask = (~in_vals.isna() & ~out_vals.isna())
                    if valid_mask.sum() < 2:
                        continue

                    ps = prefix_sum[valid_mask].values
                    ov = out_vals[valid_mask].values

                    # Solve for a and b using linear regression
                    n = len(ps)
                    sum_x = ps.sum()
                    sum_y = ov.sum()
                    sum_xy = (ps * ov).sum()
                    sum_x2 = (ps * ps).sum()

                    denom = n * sum_x2 - sum_x * sum_x
                    if abs(denom) < 1e-10:
                        continue

                    a = (n * sum_xy - sum_x * sum_y) / denom
                    b = (sum_y - a * sum_x) / n

                    # Verify this works for all points
                    predicted = a * ps + b
                    if np.allclose(predicted, ov, rtol=1e-5, atol=1e-8):
                        # Determine agg_type
                        if abs(a - 1.0) < 1e-5 and abs(b) < 1e-5:
                            agg_type = "sum"
                        elif abs(a) < 1e-5:
                            agg_type = "count"
                        else:
                            agg_type = "sum"  # General linear

                        transforms[out_col] = PrefixTransform(
                            output_col=out_col,
                            input_col=in_col,
                            agg_type=agg_type,
                            a=float(a),
                            b=float(b)
                        )
                        break
                except Exception:
                    continue

    return transforms


def infer_sliding_window_transform(input_df: pd.DataFrame, output_df: pd.DataFrame, max_w: int = 64) -> Dict[str, SlidingWindowTransform]:
    """Detect sliding window transforms."""
    transforms = {}
    input_cols = set(input_df.columns.tolist())

    for out_col in output_df.columns:
        if out_col in input_cols:
            continue

        for in_col in input_cols:
            if in_col not in output_df.columns:
                try:
                    in_vals = pd.to_numeric(input_df[in_col], errors='coerce').fillna(0)
                    out_vals = pd.to_numeric(output_df[out_col], errors='coerce')

                    if len(in_vals) != len(out_vals):
                        continue
                    if in_vals.isna().all() or out_vals.isna().all():
                        continue

                    # Try different window sizes
                    for w in range(1, min(max_w, len(in_vals)) + 1):
                        # Compute sliding window sums
                        rolling = in_vals.rolling(window=w, min_periods=1).sum()
                        valid_mask = (~in_vals.isna() & ~out_vals.isna())

                        if valid_mask.sum() < w:
                            continue

                        rv = rolling[valid_mask].values
                        ov = out_vals[valid_mask].values

                        # Try a * window_sum + b
                        n = len(rv)
                        sum_x = rv.sum()
                        sum_y = ov.sum()
                        sum_xy = (rv * ov).sum()
                        sum_x2 = (rv * rv).sum()

                        denom = n * sum_x2 - sum_x * sum_x
                        if abs(denom) < 1e-10:
                            continue

                        a = (n * sum_xy - sum_x * sum_y) / denom
                        b = (sum_y - a * sum_x) / n

                        predicted = a * rv + b
                        if np.allclose(predicted, ov, rtol=1e-5, atol=1e-8):
                            if abs(a - 1.0/w) < 1e-5:
                                agg_type = "mean"
                            elif abs(a - 1.0) < 1e-5 and abs(b) < 1e-5:
                                agg_type = "sum"
                            else:
                                agg_type = "sum"

                            transforms[out_col] = SlidingWindowTransform(
                                output_col=out_col,
                                input_col=in_col,
                                agg_type=agg_type,
                                window_size=w
                            )
                            break
                except Exception:
                    continue

    return transforms


def infer_state_machine_transform(input_df: pd.DataFrame, output_df: pd.DataFrame) -> Dict[str, StateMachineTransform]:
    """Detect state machine / segment labeling transforms."""
    transforms = {}
    input_cols = set(input_df.columns.tolist())

    for out_col in output_df.columns:
        if out_col in input_cols:
            continue

        out_vals = output_df[out_col]
        unique_states = sorted(out_vals.unique(), key=str)
        if len(unique_states) > 16:
            continue

        # Check if output changes in step with a condition on some input column
        for in_col in input_cols:
            if in_col not in output_df.columns:
                try:
                    in_vals = input_df[in_col]
                    out_vals_numeric = pd.to_numeric(output_df[out_col], errors='coerce')

                    # Check if segment_id increments when a condition is met
                    changes = out_vals_numeric.diff().fillna(0) != 0
                    change_indices = changes[changes].index.tolist()

                    if len(change_indices) > 0:
                        # Check what condition triggers the change
                        transition_rules = []
                        initial_state = out_vals_numeric.iloc[0]

                        # See what happens at each boundary
                        for idx in change_indices:
                            if idx > 0:
                                prev_in = str(in_vals.iloc[idx-1])
                                curr_in = str(in_vals.iloc[idx])
                                prev_out = out_vals_numeric.iloc[idx-1]
                                curr_out = out_vals_numeric.iloc[idx]

                                # Check if input value changed
                                if prev_in != curr_in:
                                    transition = StateTransition(
                                        condition_col=in_col,
                                        condition_op="!=",
                                        condition_value=None,  # Any change
                                        new_state=curr_out
                                    )
                                    transition_rules.append(transition)
                                else:
                                    # Check for threshold crossing
                                    try:
                                        prev_num = float(prev_in)
                                        curr_num = float(curr_in)
                                        # Determine if there's a threshold
                                        threshold = (prev_num + curr_num) / 2
                                        if curr_out != prev_out:
                                            transition = StateTransition(
                                                condition_col=in_col,
                                                condition_op=">" if curr_num > prev_num else "<",
                                                condition_value=threshold,
                                                new_state=curr_out
                                            )
                                            transition_rules.append(transition)
                                    except ValueError:
                                        pass

                        if len(transition_rules) > 0 or len(change_indices) <= 4:
                            transforms[out_col] = StateMachineTransform(
                                output_col=out_col,
                                input_cols=[in_col],
                                initial_state=initial_state,
                                transitions=transition_rules if transition_rules else [],
                                increment_on_col=in_col if len(change_indices) > 0 else None
                            )
                            break
                except Exception:
                    continue

    return transforms


def infer_neighbor_filter(input_df: pd.DataFrame, output_df: pd.DataFrame) -> List[NeighborFilter]:
    """Detect neighbor-based filtering patterns."""
    filters = []

    if len(output_df) == len(input_df):
        return filters

    # Find which rows were dropped
    input_df = input_df.copy().reset_index(drop=True)
    output_df = output_df.copy().reset_index(drop=True)

    common_cols = set(input_df.columns) & set(output_df.columns)

    kept_indices = set()
    used = set()

    for out_idx, out_row in output_df.iterrows():
        for in_idx, in_row in input_df.iterrows():
            if in_idx in used:
                continue
            matches = True
            for col in common_cols:
                if str(in_row[col]) != str(out_row[col]):
                    matches = False
                    break
            if matches:
                kept_indices.add(in_idx)
                used.add(in_idx)
                break

    dropped_indices = set(range(len(input_df))) - kept_indices

    if not dropped_indices:
        return filters

    # Check if dropped rows have specific patterns relative to neighbors
    input_cols = set(input_df.columns)

    for drop_idx in sorted(dropped_indices):
        # Check self condition
        for col in input_cols:
            if col not in common_cols:
                continue
            drop_val = str(input_df.loc[drop_idx, col])

            # Check if this value only appears in dropped rows
            kept_vals = set()
            for idx in kept_indices:
                kept_vals.add(str(input_df.loc[idx, col]))

            if drop_val not in kept_vals:
                filters.append(NeighborFilter(
                    depends_on_next=False,
                    depends_on_prev=False,
                    condition_on_self=(col, "==", drop_val),
                    keep_if_matches=False
                ))

        # Check next row condition
        if drop_idx + 1 < len(input_df):
            for col in input_cols:
                if col not in common_cols:
                    continue
                next_val = str(input_df.loc[drop_idx + 1, col])
                if next_val in kept_vals:
                    filters.append(NeighborFilter(
                        depends_on_next=True,
                        depends_on_prev=False,
                        condition_on_next=(col, "==", next_val),
                        condition_on_self=(col, "==", drop_val) if str(input_df.loc[drop_idx, col]) not in kept_vals else None,
                        keep_if_matches=False
                    ))

        # Check prev row condition
        if drop_idx > 0:
            for col in input_cols:
                if col not in common_cols:
                    continue
                prev_val = str(input_df.loc[drop_idx - 1, col])
                filters.append(NeighborFilter(
                    depends_on_next=False,
                    depends_on_prev=True,
                    condition_on_prev=(col, "==", prev_val),
                    keep_if_matches=False
                ))

    return filters


# Code generation

def python_class_body(trans: InferredTransformation, ext: str) -> str:
    """Generate Python class body for DynamicPreprocessor."""

    # Header
    body = f'''
import csv
import json
import os
from pathlib import Path
from typing import Iterator, Dict, Any, Optional


class DynamicPreprocessor:
    """Generated preprocessor that infers and applies transformations from sample data."""

    def __init__(self, buffer: int, cache_dir: Optional[str] = None):
        self.buffer = buffer
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _get_cache_path(self, filepath: str) -> Path:
        if not self.cache_dir:
            raise ValueError("cache_dir not set")
        h = hashlib.sha256(filepath.encode()).hexdigest()[:16]
        return self.cache_dir / ("cache_" + h + ".json")

    def _apply_transformation(self, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Apply the inferred transformation to a single row. Returns None if row should be filtered."""
'''

    # Add filter logic - restructure as early-return pattern
    if trans.filter_predicates:
        body += '        # Filter predicates - row is kept only if all conditions pass\n'
        for pred in trans.filter_predicates:
            val = json.dumps(pred.value)
            op_map = {
                "==": "==",
                "!=": "!=",
                "<": "<",
                "<=": "<=",
                ">": ">",
                ">=": ">="
            }
            py_op = op_map.get(pred.op, pred.op)
            if pred.children:
                # OR predicate: at least one must match
                body += f'        # Check if row matches at least one allowed value for {pred.col}\n'
                body += '        passed = False\n'
                body += f'        if row.get("{pred.col}", "") {py_op} {val}:\n'
                body += '            passed = True\n'
                for child in pred.children:
                    cval = json.dumps(child.value)
                    body += f'        if not passed and row.get("{pred.col}", "") {py_op} {cval}:\n'
                    body += '            passed = True\n'
                body += '        if not passed:\n'
                body += '            return None\n'
            else:
                # Simple predicate
                body += f'        if not (row.get("{pred.col}", "") {py_op} {val}):\n'
                body += '            return None\n'
        body += '\n'

    # Add column transformation
    body += '        # Apply column transformations\n'
    body += '        out_row = {{}}\n'
    for out_col, t in trans.col_transforms.items():
        if t.transform_type == "identity":
            body += f'        out_row["{out_col}"] = row.get("{t.input_col}", "")\n'
        elif t.transform_type == "rename":
            body += f'        out_row["{out_col}"] = row.get("{t.input_col}", "")\n'
        elif t.transform_type == "const":
            v = json.dumps(t.constant_value)
            body += f'        out_row["{out_col}"] = {v}\n'
        elif t.transform_type == "trim":
            body += f'        out_row["{out_col}"] = str(row.get("{t.input_col}", "")).strip()\n'
        elif t.transform_type == "lower":
            body += f'        out_row["{out_col}"] = str(row.get("{t.input_col}", "")).lower()\n'
        elif t.transform_type == "upper":
            body += f'        out_row["{out_col}"] = str(row.get("{t.input_col}", "")).upper()\n'
        elif t.transform_type == "linear":
            body += f'        try:\n'
            body += f'            val = float(row.get("{t.input_col}", 0))\n'
            body += f'            out_row["{out_col}"] = {t.a} * val + {t.b}\n'
            body += f'            if out_row["{out_col}"] == int(out_row["{out_col}"]):\n'
            body += f'                out_row["{out_col}"] = int(out_row["{out_col}"])\n'
            body += f'        except:\n'
            body += f'            out_row["{out_col}"] = row.get("{t.input_col}", "")\n'
        else:
            body += f'        out_row["{out_col}"] = row.get("{t.input_col}", "")\n'

    # Generate stateful transform handling code
    body += _generate_stateful_transform_code(trans)

    return body


def _generate_stateful_transform_code(trans: InferredTransformation) -> str:
    """Generate code for stateful transforms."""
    code = '\n'
    code += '    # Stateful transform support\n'
    code += '    _prefix_values: Dict[str, float] = {}\n'
    code += '    _sliding_window_buffers: Dict[str, List] = {}\n'
    code += '    _sliding_window_sizes: Dict[str, int] = {}\n'
    code += '    _state_values: Dict[str, Any] = {}\n'
    code += '    _previous_rows: Dict[str, Dict[str, Any]] = {}  # For neighbor-based filtering\n'
    code += '    _has_previous: Dict[str, bool] = {}\n\n'

    # Initialize state from cache if provided
    code += '    def _restore_state(self, cache_state: Dict[str, Any]) -> None:\n'
    code += '        if "prefix_values" in cache_state:\n'
    code += '            self._prefix_values = {k: float(v) for k, v in cache_state["prefix_values"].items()}\n'
    code += '        if "sliding_window_buffers" in cache_state:\n'
    code += '            self._sliding_window_buffers = cache_state["sliding_window_buffers"]\n'
    code += '        if "sliding_window_sizes" in cache_state:\n'
    code += '            self._sliding_window_sizes = cache_state["sliding_window_sizes"]\n'
    code += '        if "state_values" in cache_state:\n'
    code += '            self._state_values = cache_state["state_values"]\n\n'

    code += '    def _save_state(self) -> Dict[str, Any]:\n'
    code += '        return {\n'
    code += '            "prefix_values": dict(self._prefix_values),\n'
    code += '            "sliding_window_buffers": dict(self._sliding_window_buffers),\n'
    code += '            "sliding_window_sizes": dict(self._sliding_window_sizes),\n'
    code += '            "state_values": dict(self._state_values),\n'
    code += '        }\n\n'

    # Stateful column transformation helper
    code += '    def _apply_stateful_transform(self, row: Dict[str, Any], col: str) -> Any:\n'
    code += '        """Apply stateful transform for a specific column. Returns the transformed value.\n'
    code += '        """\n'

    # Prefix transforms
    if trans.prefix_transforms:
        for pt in trans.prefix_transforms.values():
            code += f'        if col == "{pt.output_col}":\n'
            code += f'            input_val = float(row.get("{pt.input_col}", 0) or 0)\n'
            code += f'            if "{pt.output_col}" not in self._prefix_values:\n'
            code += f'                self._prefix_values["{pt.output_col}"] = 0.0\n'
            code += f'            self._prefix_values["{pt.output_col}"] += input_val\n'
            code += f'            result = {pt.a} * self._prefix_values["{pt.output_col}"] + {pt.b}\n'
            code += f'            if result == int(result):\n'
            code += f'                return int(result)\n'
            code += f'            return result\n'

    # Sliding window transforms
    if trans.sliding_window_transforms:
        for swt in trans.sliding_window_transforms.values():
            code += f'        if col == "{swt.output_col}":\n'
            code += f'            input_val = float(row.get("{swt.input_col}", 0) or 0)\n'
            code += f'            if "{swt.output_col}" not in self._sliding_window_buffers:\n'
            code += f'                self._sliding_window_buffers["{swt.output_col}"] = []\n'
            code += f'            if "{swt.output_col}" not in self._sliding_window_sizes:\n'
            code += f'                self._sliding_window_sizes["{swt.output_col}"] = {swt.window_size}\n\n'
            code += f'            buf = self._sliding_window_buffers["{swt.output_col}\"]\n'
            code += f'            w = self._sliding_window_sizes["{swt.output_col}\"]\n'
            code += f'            buf.append(input_val)\n'
            code += f'            if len(buf) > w:\n'
            code += f'                buf.pop(0)\n\n'
            # Compute the aggregation
            if swt.agg_type == "mean":
                code += f'            result = sum(buf) / len(buf)\n'
            elif swt.agg_type == "sum":
                code += f'            result = sum(buf)\n'
            elif swt.agg_type == "count":
                code += f'            result = len(buf)\n'
            else:
                code += f'            result = sum(buf) / len(buf)  # default to mean\n'
            code += f'            if result == int(result):\n'
            code += f'                return int(result)\n'
            code += f'            return result\n'

    # State machine transforms
    if trans.state_machine_transforms:
        for smt in trans.state_machine_transforms.values():
            code += f'        if col == "{smt.output_col}":\n'
            code += f'            if "{smt.output_col}" not in self._state_values:\n'
            code += f'                self._state_values["{smt.output_col}"] = {json.dumps(smt.initial_state)}\n'
            code += f'            state = self._state_values["{smt.output_col}\"]\n'
            code += f'            new_state = self._update_state_machine_{smt.output_col}(row, state)\n'
            code += f'            self._state_values["{smt.output_col}"] = new_state\n'
            code += f'            return new_state\n'

    code += '        return None\n\n'

    # Generate state machine update methods
    for smt in trans.state_machine_transforms.values():
        code += f'    def _update_state_machine_{smt.output_col}(self, row: Dict[str, Any], state: Any) -> Any:\n'
        code += f'        """Update state machine for {smt.output_col} column."""\n'
        code += f'        # Simple increment-based state machine\n'
        if smt.increment_on_col:
            col = smt.increment_on_col
            code += f'        input_val = row.get("{col}\")\n'
            code += f'        if self._has_previous.get("{smt.output_col}\"):\n'
            code += f'            prev_val = self._previous_rows.get("{smt.output_col}\")\n'
            code += f'            if str(input_val) != str(prev_val):\n'
            code += f'                return state + 1\n'
            code += f'        self._previous_rows["{smt.output_col}"] = input_val\n'
            code += f'        self._has_previous["{smt.output_col}\"] = True\n'
            code += f'        return state\n'

    # Neighbor-based filtering
    if trans.neighbor_filters:
        code += '\n'
        code += '    def _check_neighbor_filters(self, row: Dict[str, Any], next_row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:\n'
        code += '        """Check neighbor-based filters. Returns row if kept, None if filtered.\n'
        code += '        """\n'
        code += '        for nf in self._neighbor_filter_list:\n'
        code += '            if nf.get("depends_on_next"):\n'
        code += '                if next_row is None:\n'
        code += '                    return None  # Need to see next row, delay decision\n'
        code += '            # Check conditions and return None if filtered\n'

    return code


    # Add streaming processing

    if ext == "json":
        body += f'''
        # For JSON, read all and process
        with open(path, 'r', encoding='utf-8') as f:
            all_rows = json.load(f)

        for i, row in enumerate(all_rows):
            if i < processed_up_to:
                # Already processed, yield from cache
                cached = cache_state.get("rows", {{}}).get(str(i))
                if cached:
                    yield cached
                continue

            result = self._apply_transformation(row)
            if result is not None:
                buffer.append(result)
                if len(buffer) >= self.buffer:
                    for buf_row in buffer:
                        yield buf_row
                    buffer = []

        # Yield remaining
        for row in buffer:
            yield row

        # Update cache
        if cache_path:
            cache_state["processed_up_to"] = len(all_rows)
            with open(cache_path, 'w') as f:
                json.dump(cache_state, f)
'''
    else:
        body += f'''
        row_iter = self._read_rows(path)

        for i, row in enumerate(row_iter):
            if i < processed_up_to:
                # Already processed, yield from cache
                cached = cache_state.get("rows", {{}}).get(str(i))
                if cached:
                    yield cached
                continue

            result = self._apply_transformation(row)
            if result is not None:
                buffer.append(result)
                if len(buffer) >= self.buffer:
                    for buf_row in buffer:
                        yield buf_row
                    buffer = []

        # Yield remaining
        for row in buffer:
            yield row

        # Update cache
        if cache_path:
            if "rows" not in cache_state:
                cache_state["rows"] = {{}}
            # Cache the transformed rows we just generated
            with open(path, 'r', encoding='utf-8') as f:
                reader = self._reader_for_format(path)
                all_input_rows = list(reader)
            for j in range(processed_up_to, len(all_input_rows)):
                row = all_input_rows[j]
                result = self._apply_transformation(row)
                if result is not None:
                    cache_state["rows"][str(j)] = result
            cache_state["processed_up_to"] = len(all_input_rows)
            with open(cache_path, 'w') as f:
                json.dump(cache_state, f)
'''

    # Add _read_rows method
    body += f'''
    def _reader_for_format(self, path: Path):
        """Return appropriate reader for file format."""
        ext = path.suffix.lstrip(".")
        if ext == "csv":
            return csv.DictReader(open(path, "r", encoding="utf-8"))
        elif ext == "tsv":
            return csv.DictReader(open(path, "r", encoding="utf-8"), delimiter="\\t")
        elif ext == "jsonl":
            for line in open(path, "r", encoding="utf-8"):
                if line.strip():
                    yield json.loads(line)
            return
        else:
            raise ValueError(f"Unsupported format: {{ext}}")

    def _read_rows(self, path: Path):
        """Read rows from file."""
        ext = path.suffix.lstrip(".")
        if ext == "json":
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    for row in data:
                        yield row
                else:
                    yield data
        else:
            reader = self._reader_for_format(path)
            for row in reader:
                # Convert numeric values
                converted = {{}}
                for k, v in row.items():
                    if v is None or v == "":
                        converted[k] = None
                    else:
                        # Try to convert to int/float
                        try:
                            if '.' in str(v):
                                converted[k] = float(v)
                            else:
                                converted[k] = int(v)
                        except ValueError:
                            converted[k] = v.strip() if isinstance(v, str) else v
                yield converted
'''

    return body


def generate_python_module(module_name: str, trans: InferredTransformation, ext: str) -> Tuple[str, Dict[str, str]]:
    """Generate Python package files. Returns (init_content, {filename: content})."""

    class_body = python_class_body(trans, ext)

    init_content = f'''"""Generated dynamic preprocessing module: {module_name}"""
from .dynamic_buffer import DynamicPreprocessor

__all__ = ["DynamicPreprocessor"]
'''

    files = {
        "__init__.py": init_content,
        "dynamic_buffer.py": class_body
    }

    return init_content, files


def generate_javascript_module(module_name: str, trans: InferredTransformation, ext: str) -> Dict[str, str]:
    """Generate JavaScript module files."""

    # Build filter logic
    filter_js = ""
    if trans.filter_predicates:
        filter_js = "        // Filter predicates\n"
        filter_js += "        let filterPassed = true;\n"
        for pred in trans.filter_predicates:
            val = json.dumps(pred.value)
            op_map = {
                "==": "===",
                "!=": "!==",
                "<": "<",
                "<=": "<=",
                ">": ">",
                ">=": ">="
            }
            js_op = op_map.get(pred.op, pred.op)
            filter_js += f'        if (row["{pred.col}"] {js_op} {val}) {{\n'
            filter_js += '            filterPassed = false;\n'
            filter_js += '        }\n'
        filter_js += '        if (!filterPassed) return null;\n\n'

    # Build column transformation
    transform_js = "        // Apply column transformations\n"
    transform_js += "        const outRow = {};\n"
    for out_col, t in trans.col_transforms.items():
        if t.transform_type == "identity":
            transform_js += f'        outRow["{out_col}"] = row["{t.input_col}"];\n'
        elif t.transform_type == "rename":
            transform_js += f'        outRow["{out_col}"] = row["{t.input_col}"];\n'
        elif t.transform_type == "const":
            v = json.dumps(t.constant_value)
            transform_js += f'        outRow["{out_col}"] = {v};\n'
        elif t.transform_type == "trim":
            transform_js += f'        outRow["{out_col}"] = String(row["{t.input_col}"] || "").trim();\n'
        elif t.transform_type == "lower":
            transform_js += f'        outRow["{out_col}"] = String(row["{t.input_col}"] || "").toLowerCase();\n'
        elif t.transform_type == "upper":
            transform_js += f'        outRow["{out_col}"] = String(row["{t.input_col}"] || "").toUpperCase();\n'
        elif t.transform_type == "linear":
            transform_js += f'        try {{\n'
            transform_js += f'            const val = parseFloat(row["{t.input_col}"] || 0);\n'
            transform_js += f'            outRow["{out_col}"] = {t.a} * val + {t.b};\n'
            transform_js += f'            if (Number.isInteger(outRow["{out_col}"])) {{\n'
            transform_js += f'                outRow["{out_col}"] = Math.floor(outRow["{out_col}"]);\n'
            transform_js += f'            }}\n'
            transform_js += f'        }} catch(e) {{\n'
            transform_js += f'            outRow["{out_col}"] = row["{t.input_col}\"];\n'
            transform_js += f'        }}\n'
        else:
            transform_js += f'        outRow["{out_col}"] = row["{t.input_col}"];\n'

    transform_js += "        return outRow;\n"

    js_code = f'''const fs = require('fs');
const path = require('path');

class DynamicPreprocessor {{
    constructor({{ buffer, cache_dir = null }}) {{
        this.buffer = buffer;
        this.cache_dir = cache_dir;
        if (this.cache_dir) {{
            if (!fs.existsSync(this.cache_dir)) {{
                fs.mkdirSync(this.cache_dir, {{ recursive: true }});
            }}
        }}
    }}

    _getCachePath(filepath) {{
        if (!this.cache_dir) throw new Error("cache_dir not set");
        const crypto = require('crypto');
        const h = crypto.createHash('sha256').update(filepath).digest('hex').substring(0, 16);
        return path.join(this.cache_dir, `cache_${{h}}.json`);
    }}

    _applyTransformation(row) {{
{filter_js}{transform_js}
    }}

    _readRows(filePath) {{
        const ext = path.extname(filePath).slice(1);
        if (ext === 'json') {{
            const data = JSON.parse(fs.readFileSync(filePath, 'utf8'));
            if (Array.isArray(data)) return data;
            return [data];
        }} else if (ext === 'jsonl') {{
            const lines = fs.readFileSync(filePath, 'utf8').split('\\n').filter(l => l.trim());
            return lines.map(l => JSON.parse(l));
        }} else if (ext === 'csv' || ext === 'tsv') {{
            const content = fs.readFileSync(filePath, 'utf8');
            const delimiter = ext === 'tsv' ? '\\t' : ',';
            const lines = content.split('\\n').filter(l => l.trim());
            if (lines.length === 0) return [];
            const headers = lines[0].split(delimiter);
            return lines.slice(1).map(line => {{
                const values = line.split(delimiter);
                const row = {{}};
                headers.forEach((h, i) => {{
                    row[h] = values[i] !== undefined ? values[i] : '';
                }});
                return row;
            }});
        }}
        throw new Error(`Unsupported format: ${{ext}}`);
    }}

    *[Symbol.iterator]({{
        const self = this;
        return {{
            next() {{
                // This is a simplified iterator - actual implementation uses __call__
                return {{ done: true, value: null }};
            }}
        }};
    }})

    __call__(filePath) {{
        const ext = path.extname(filePath).slice(1);
        const cachePath = this.cache_dir ? this._getCachePath(filePath) : null;

        let cacheState = {{}};
        let processedUpTo = 0;
        if (cachePath && fs.existsSync(cachePath)) {{
            cacheState = JSON.parse(fs.readFileSync(cachePath, 'utf8'));
            processedUpTo = cacheState.processed_up_to || 0;
        }}

        const buffer = [];
        const allRows = this._readRows(filePath);

        const transformRow = (row, idx) => {{
            if (idx < processedUpTo) {{
                const cached = cacheState.rows && cacheState.rows[idx];
                return cached;
            }}
            return this._applyTransformation(row);
        }};

        const generator = function*() {{
            for (let i = 0; i < allRows.length; i++) {{
                const result = transformRow(allRows[i], i);
                if (result !== null) {{
                    if (buffer.length >= self.buffer) {{
                        for (const bufRow of buffer) yield bufRow;
                        buffer.length = 0;
                    }}
                    buffer.push(result);
                }}
            }}
            for (const row of buffer) yield row;
        }};

        const gen = generator();

        const result = {{
            *[Symbol.iterator]() {{
                yield* gen;
            }}
        }};

        // Update cache after full processing
        if (cachePath) {{
            const newRows = {{}};
            for (let i = processedUpTo; i < allRows.length; i++) {{
                const result = transformRow(allRows[i], i);
                if (result !== null) newRows[i] = result;
            }}
            cacheState.rows = {{ ...cacheState.rows, ...newRows }};
            cacheState.processed_up_to = allRows.length;
            fs.writeFileSync(cachePath, JSON.stringify(cacheState));
        }}

        return result;
    }}
}}

// Factory function for compatibility
function DynamicPreprocessorFactory(options) {{
    return new DynamicPreprocessor(options);
}}

module.exports = {{ DynamicPreprocessor, DynamicPreprocessorFactory }};
'''

    return {
        "index.js": f"exports.DynamicPreprocessor = require('./dynamic_buffer').DynamicPreprocessor;",
        "dynamic_buffer.js": js_code
    }


# Main entry point

def main():
    parser = argparse.ArgumentParser(
        description="Generate a DynamicPreprocessor module from sample input/output pairs."
    )
    parser.add_argument("module_name", help="Name of the generated module/package")
    parser.add_argument("--output", required=True, help="Output directory for generated module")
    parser.add_argument("--sample", required=True, help="Directory containing input.{ext} and output.{ext}")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--python", action="store_true", help="Generate Python module")
    group.add_argument("--javascript", action="store_true", help="Generate JavaScript module")

    args = parser.parse_args()

    sample_dir = Path(args.sample)
    output_dir = Path(args.output)
    module_name = args.module_name

    # Validate sample directory
    if not sample_dir.exists():
        print(f"Error: Sample directory '{sample_dir}' does not exist", file=sys.stderr)
        sys.exit(1)

    ext = detect_extension(sample_dir)
    if ext not in SUPPORTED_EXTS:
        print(f"Error: Unsupported extension '.{ext}'", file=sys.stderr)
        sys.exit(1)

    # Load input and output data
    input_path = sample_dir / f"input.{ext}"
    output_path = sample_dir / f"output.{ext}"

    if not input_path.exists():
        print(f"Error: Missing input file: {input_path}", file=sys.stderr)
        sys.exit(1)
    if not output_path.exists():
        print(f"Error: Missing output file: {output_path}", file=sys.stderr)
        sys.exit(1)

    input_df = load_data(input_path)
    output_df = load_data(output_path)

    print(f"Loaded {len(input_df)} input rows with columns: {list(input_df.columns)}")
    print(f"Loaded {len(output_df)} output rows with columns: {list(output_df.columns)}")

    # Infer transformation
    trans = infer_transformation(input_df, output_df)

    print(f"\nInferred transformation:")
    print(f"  Dropped columns: {trans.original_cols - trans.keep_cols}")
    print(f"  Filter predicates: {len(trans.filter_predicates)}")
    for out_col, t in trans.col_transforms.items():
        print(f"  {out_col}: {t.transform_type} <- {t.input_col}")

    # Generate code
    if args.python:
        init_content, files = generate_python_module(module_name, trans, ext)
        module_dir = output_dir / module_name
        module_dir.mkdir(parents=True, exist_ok=True)
        for filename, content in files.items():
            filepath = module_dir / filename
            filepath.write_text(content, encoding="utf-8")
        print(f"\nGenerated Python package at: {module_dir}/")
    else:
        files = generate_javascript_module(module_name, trans, ext)
        # For JS, create the module directory with index.js and dynamic_buffer.js
        # We'll output to a file named <module_name>.js as a single file for simplicity
        # But the spec says --output/<module_name>/... so we need a directory
        module_dir = output_dir / module_name
        module_dir.mkdir(parents=True, exist_ok=True)
        for filename, content in files.items():
            filepath = module_dir / filename
            filepath.write_text(content, encoding="utf-8")
        print(f"\nGenerated JavaScript module at: {module_dir}/")

    print("\nDone! You can now import and use the generated module.")


if __name__ == "__main__":
    main()
