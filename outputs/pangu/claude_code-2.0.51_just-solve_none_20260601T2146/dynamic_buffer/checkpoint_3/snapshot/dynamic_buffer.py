#!/usr/bin/env python3
"""Dynamic Buffer Code Generator.

Given a sample input/output pair, infers transformations and generates a
DynamicPreprocessor module.
"""

import argparse
import csv
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Tuple, Any, Dict, Optional, List


# ==============================================================================
# Data Models
# ==============================================================================

class DataType(Enum):
    CSV = "csv"
    TSV = "tsv"
    JSONL = "jsonl"
    JSON = "json"


# ==============================================================================
# Stateful Transform Types (Part 2 Extensions)
# ==============================================================================

class StatefulTransformType(Enum):
    PREFIX_SUM = "prefix_sum"
    PREFIX_COUNT = "prefix_count"
    PREFIX_AVG = "prefix_avg"
    WINDOW_SUM = "window_sum"
    WINDOW_AVG = "window_avg"
    WINDOW_COUNT = "window_count"
    STATE_MACHINE = "state_machine"
    NEIGHBOR_FILTER = "neighbor_filter"


@dataclass
class PrefixTransform:
    """Cumulative aggregation over all previous rows."""
    input_column: str
    output_column: str
    transform_type: str  # "sum", "count", "avg"
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class WindowTransform:
    """Sliding window aggregation over last W rows."""
    input_column: str
    output_column: str
    window_size: int  # 1-64
    transform_type: str  # "sum", "avg", "count"
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class StateMachineTransform:
    """Finite state machine for sequence labeling."""
    input_column: str
    output_column: str
    states: List[str]  # up to 16 states
    transitions: List[Dict[str, Any]]  # state transition rules
    initial_state: str
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class NeighborFilterTransform:
    """Filter based on neighboring rows (lookahead/lookbehind)."""
    condition: str  # condition referencing neighbor
    direction: str  # "next" or "prev"
    params: Dict[str, Any] = field(default_factory=dict)


# ==============================================================================
# Core Transform Models (Part 1 base)
# ==============================================================================

@dataclass
class ColumnTransform:
    """Represents a column transformation."""
    input_column: Optional[str]
    transform_type: str
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FilterPredicate:
    """Represents a filter predicate."""
    condition: str


# ==============================================================================
# Data Loading
# ==============================================================================

def detect_data_format(filepath: str) -> DataType:
    ext = Path(filepath).suffix.lower().lstrip(".")
    if ext == "csv":
        return DataType.CSV
    if ext == "tsv":
        return DataType.TSV
    if ext == "jsonl":
        return DataType.JSONL
    if ext == "json":
        return DataType.JSON
    raise ValueError(f"Unsupported file extension: {ext}")


def load_input_data(filepath: str) -> List[Dict[str, Any]]:
    data_type = detect_data_format(filepath)

    if data_type == DataType.CSV:
        with open(filepath, "r", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    if data_type == DataType.TSV:
        with open(filepath, "r", encoding="utf-8") as f:
            return list(csv.DictReader(f, delimiter="\t"))
    if data_type == DataType.JSONL:
        rows = []
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return rows
    if data_type == DataType.JSON:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else [data]
    raise ValueError(f"Unsupported format: {data_type}")


def _infer_value_type(value: str) -> Any:
    if value is None or value == "":
        return None
    lower = value.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


# ==============================================================================
# Transformation Inference
# ==============================================================================

def infer_transforms(
    input_rows: List[Dict[str, Any]],
    output_rows: List[Dict[str, Any]],
) -> Tuple[Dict[str, ColumnTransform], Optional[FilterPredicate]]:
    if not input_rows:
        return {}, FilterPredicate("False")

    output_indices: List[int] = []
    input_idx = 0

    for out_row in output_rows:
        while input_idx < len(input_rows):
            in_row = input_rows[input_idx]
            match = all(
                str(out_row.get(k)) == str(in_row.get(k)) if in_row.get(k) is not None
                else out_row.get(k) is None
                for k in out_row
            )
            if match:
                output_indices.append(input_idx)
                input_idx += 1
                break
            input_idx += 1

    output_columns = set(output_rows[0].keys()) if output_rows else set()
    input_columns = set(input_rows[0].keys()) if input_rows else set()

    kept_indices = set(output_indices)
    all_indices = set(range(len(input_rows)))
    filter_pred = _build_filter_predicate(input_rows, kept_indices)

    column_transforms: Dict[str, ColumnTransform] = {}

    for out_col in output_columns:
        possible_input_cols = [c for c in input_columns
                              if c.lower() == out_col.lower() or c == out_col]

        if not possible_input_cols:
            col_transform = _infer_computed_column(out_col, input_rows, output_rows, output_indices)
            if col_transform:
                column_transforms[out_col] = col_transform
                continue
            values = {row.get(out_col) for row in output_rows}
            if len(values) == 1:
                column_transforms[out_col] = ColumnTransform(
                    input_column=None,
                    transform_type="const",
                    params={"value": list(values)[0]},
                )
            else:
                column_transforms[out_col] = ColumnTransform(
                    input_column=None,
                    transform_type="drop",
                    params={},
                )
        else:
            in_col = possible_input_cols[0]
            col_transform = _infer_column_transform(in_col, out_col, input_rows, output_rows, output_indices)
            column_transforms[out_col] = col_transform

    for in_col in input_columns - output_columns:
        if in_col not in {ct.input_column for ct in column_transforms.values() if ct.input_column}:
            column_transforms[in_col] = ColumnTransform(
                input_column=in_col,
                transform_type="drop",
                params={},
            )

    return column_transforms, filter_pred


def _build_filter_predicate(input_rows: List[Dict[str, Any]], kept_indices: set) -> Optional[FilterPredicate]:
    if kept_indices == set(range(len(input_rows))):
        return None

    for row_idx in kept_indices:
        if row_idx >= len(input_rows):
            continue
        row = input_rows[row_idx]
        for col, val in row.items():
            kept_values = [input_rows[i].get(col) for i in kept_indices]
            dropped_values = [input_rows[i].get(col) for i in range(len(input_rows)) if i not in kept_indices]
            if kept_values and dropped_values:
                if all(v == val for v in kept_values) and not any(v == val for v in dropped_values):
                    return FilterPredicate(f"{col} == {repr(val)}")
                if all(v != val for v in kept_values) and any(v == val for v in dropped_values):
                    return FilterPredicate(f"{col} != {repr(val)}")

    return FilterPredicate(f"row_index in {sorted(kept_indices)}")


def _infer_column_transform(
    in_col: str,
    out_col: str,
    input_rows: List[Dict[str, Any]],
    output_rows: List[Dict[str, Any]],
    kept_indices: List[int],
) -> ColumnTransform:
    in_values = [input_rows[i].get(in_col) for i in kept_indices]
    out_values = [output_rows[i].get(out_col) for i in range(len(output_rows))] if output_rows else []

    if in_values == out_values:
        return ColumnTransform(
            input_column=in_col,
            transform_type="identity" if in_col == out_col else "copy",
            params={},
        )

    numeric_transform = _infer_numeric_transform(in_values, out_values)
    if numeric_transform:
        a, b = numeric_transform
        return ColumnTransform(
            input_column=in_col,
            transform_type="numeric",
            params={"a": a, "b": b},
        )

    if all(isinstance(v, str) for v in in_values + out_values):
        if all(v.lower() == out_v for v, out_v in zip(in_values, out_values)):
            return ColumnTransform(input_column=in_col, transform_type="string_lower", params={})
        if all(v.upper() == out_v for v, out_v in zip(in_values, out_values)):
            return ColumnTransform(input_column=in_col, transform_type="string_upper", params={})
        if all(v.strip() == out_v for v, out_v in zip(in_values, out_values)):
            return ColumnTransform(input_column=in_col, transform_type="string_trim", params={})

    return ColumnTransform(input_column=in_col, transform_type="identity", params={})


def _infer_computed_column(
    out_col: str,
    input_rows: List[Dict[str, Any]],
    output_rows: List[Dict[str, Any]],
    kept_indices: List[int],
) -> Optional[ColumnTransform]:
    values = [output_rows[i].get(out_col) for i in range(len(output_rows))]
    if len(set(values)) == 1:
        return ColumnTransform(input_column=None, transform_type="const", params={"value": values[0]})

    for in_col1 in input_rows[0].keys() if input_rows else []:
        for in_col2 in input_rows[0].keys() if input_rows else []:
            if in_col1 == in_col2:
                continue
            test_values = [
                str(input_rows[i].get(in_col1, "")) + str(input_rows[i].get(in_col2, ""))
                for i in kept_indices
            ]
            out_vals = [output_rows[i].get(out_col, "") for i in range(len(output_rows))]
            if test_values == out_vals:
                return ColumnTransform(
                    input_column=None,
                    transform_type="string_concat",
                    params={"columns": [in_col1, in_col2]},
                )
    return None


def _infer_numeric_transform(in_values: List[Any], out_values: List[Any]) -> Optional[Tuple[float, float]]:
    pairs = []
    for in_val, out_val in zip(in_values, out_values):
        try:
            in_num = float(in_val) if in_val is not None else None
            out_num = float(out_val) if out_val is not None else None
            if in_num is not None and out_num is not None:
                pairs.append((in_num, out_num))
        except (ValueError, TypeError):
            continue

    if len(pairs) < 2:
        return None

    if len(pairs) == 2:
        x1, y1 = pairs[0]
        x2, y2 = pairs[1]
        if x1 == x2:
            return None
        return ((y2 - y1) / (x2 - x1), y1 - ((y2 - y1) / (x2 - x1)) * x1)

    n = len(pairs)
    sum_x = sum(x for x, y in pairs)
    sum_y = sum(y for x, y in pairs)
    sum_xy = sum(x * y for x, y in pairs)
    sum_xx = sum(x * x for x, y in pairs)

    denom = n * sum_xx - sum_x * sum_x
    if denom == 0:
        return None

    a = (n * sum_xy - sum_x * sum_y) / denom
    b = (sum_y - a * sum_x) / n
    return (a, b)


# ==============================================================================
# Stateful Transform Inference (Part 2 Extensions)
# ==============================================================================

def _detect_prefix_transform(
    input_rows: List[Dict[str, Any]],
    output_rows: List[Dict[str, Any]],
    kept_indices: List[int],
    output_col: str,
) -> Optional[PrefixTransform]:
    """Detect prefix/cumulative transforms like running sum, count, average."""
    if not input_rows or not output_rows:
        return None

    # Try to find matching numeric input column
    for in_col in input_rows[0].keys():
        try:
            in_vals = [float(input_rows[i].get(in_col, 0)) for i in kept_indices]
            out_vals = [float(output_rows[i].get(output_col, 0)) for i in range(len(output_rows))]
        except (ValueError, TypeError):
            continue

        if len(in_vals) != len(out_vals):
            continue

        # Check for prefix sum: out[i] = sum(in[0..i])
        prefix_sum = 0
        sum_match = True
        for i, (iv, ov) in enumerate(zip(in_vals, out_vals)):
            prefix_sum += iv
            if abs(prefix_sum - ov) > 0.001:
                sum_match = False
                break

        if sum_match and len(in_vals) > 1:
            return PrefixTransform(
                input_column=in_col,
                output_column=output_col,
                transform_type="sum",
                params={},
            )

        # Check for prefix count: out[i] = i + 1 (row number)
        count_match = True
        for i, ov in enumerate(out_vals):
            if abs(ov - (i + 1)) > 0.001:
                count_match = False
                break
        if count_match:
            return PrefixTransform(
                input_column=in_col,
                output_column=output_col,
                transform_type="count",
                params={},
            )

        # Check for prefix average: out[i] = mean(in[0..i])
        prefix_sum = 0
        avg_match = True
        for i, (iv, ov) in enumerate(zip(in_vals, out_vals)):
            prefix_sum += iv
            expected_avg = prefix_sum / (i + 1)
            if abs(expected_avg - ov) > 0.001:
                avg_match = False
                break

        if avg_match and len(in_vals) > 1:
            return PrefixTransform(
                input_column=in_col,
                output_column=output_col,
                transform_type="avg",
                params={},
            )

    return None


def _detect_window_transform(
    input_rows: List[Dict[str, Any]],
    output_rows: List[Dict[str, Any]],
    kept_indices: List[int],
    output_col: str,
) -> Optional[WindowTransform]:
    """Detect sliding window transforms (running mean, sum over last W rows)."""
    if not input_rows or not output_rows:
        return None

    # Try to find matching numeric input column
    for in_col in input_rows[0].keys():
        try:
            in_vals = [float(input_rows[i].get(in_col, 0)) for i in kept_indices]
            out_vals = [float(output_rows[i].get(output_col, 0)) for i in range(len(output_rows))]
        except (ValueError, TypeError):
            continue

        if len(in_vals) != len(out_vals):
            continue

        # Try window sizes 1-64
        for window_size in range(1, 65):
            # Check for window sum
            sum_match = True
            window_sum = 0
            for i, (iv, ov) in enumerate(zip(in_vals, out_vals)):
                if i < window_size:
                    # For first window_size-1 elements, window is smaller
                    window_start = max(0, i)
                else:
                    window_start = i - window_size + 1
                window_sum = sum(in_vals[window_start:i+1])
                if abs(window_sum - ov) > 0.001:
                    sum_match = False
                    break

            if sum_match and window_size <= len(in_vals):
                return WindowTransform(
                    input_column=in_col,
                    output_column=output_col,
                    window_size=window_size,
                    transform_type="sum",
                    params={},
                )

            # Check for window average
            avg_match = True
            for i, (iv, ov) in enumerate(zip(in_vals, out_vals)):
                if i < window_size:
                    window_start = max(0, i)
                else:
                    window_start = i - window_size + 1
                window_vals = in_vals[window_start:i+1]
                window_sum = sum(window_vals)
                expected_avg = window_sum / len(window_vals)
                if abs(expected_avg - ov) > 0.001:
                    avg_match = False
                    break

            if avg_match and window_size <= len(in_vals):
                return WindowTransform(
                    input_column=in_col,
                    output_column=output_col,
                    window_size=window_size,
                    transform_type="avg",
                    params={},
                )

            # Check for window count (count of rows meeting predicate - simplified)
            count_match = True
            for i in range(len(out_vals)):
                # Count should be min(i+1, window_size) if counting all rows
                expected = min(i + 1, window_size)
                if abs(out_vals[i] - expected) > 0.001:
                    count_match = False
                    break

            if count_match and window_size <= len(in_vals):
                return WindowTransform(
                    input_column=in_col,
                    output_column=output_col,
                    window_size=window_size,
                    transform_type="count",
                    params={},
                )

    return None


def _detect_state_machine_transform(
    input_rows: List[Dict[str, Any]],
    output_rows: List[Dict[str, Any]],
    kept_indices: List[int],
    output_col: str,
) -> Optional[StateMachineTransform]:
    """Detect finite state machine for sequence labeling (segment IDs, phases)."""
    if not input_rows or not output_rows:
        return None

    out_vals = [output_rows[i].get(output_col) for i in range(len(output_rows))]

    # Output must be integer/string states
    try:
        out_ints = [int(v) for v in out_vals if v is not None]
        if len(out_ints) < 2:
            return None
    except (ValueError, TypeError):
        return None

    # Look for pattern where state changes based on threshold/predicate in input column
    for in_col in input_rows[0].keys():
        # Try to find if state increments when input crosses threshold
        in_vals_numeric = []
        for i in kept_indices:
            try:
                val = float(input_rows[i].get(in_col, 0))
                in_vals_numeric.append(val)
            except (ValueError, TypeError):
                in_vals_numeric.append(None)

        if len(in_vals_numeric) < 2:
            continue

        # Build transition model: state[i] depends on state[i-1] and in_col[i]
        # Check for increment on threshold cross
        states = sorted(set(out_ints))
        if len(states) > 16:
            continue

        # Detect threshold-based transitions
        # Common pattern: stay in same state while condition is true, increment when it flips
        # For simplicity, detect if segment_id increments when value crosses threshold

        # Analyze transitions
        transitions = []
        thresholds = set()

        for i in range(1, len(out_ints)):
            prev_state = out_ints[i-1]
            curr_state = out_ints[i]
            in_val = in_vals_numeric[i]

            if in_val is not None and curr_state != prev_state:
                transitions.append({
                    "trigger": "threshold",
                    "input_value": in_val,
                    "from_state": prev_state,
                    "to_state": curr_state,
                })

        if len(transitions) > 0:
            # Determine threshold from transitions
            # All transitions should be of same "type"
            # For segment_id pattern: new segment when value crosses threshold

            # Check if all state increments are by 1
            all_increments_by_1 = True
            for i in range(1, len(out_ints)):
                if out_ints[i] - out_ints[i-1] != 0 and out_ints[i] - out_ints[i-1] != 1:
                    all_increments_by_1 = False
                    break

            if all_increments_by_1:
                # Find threshold: value above which starts new segment
                # Look for the value where state changes
                threshold_values = [
                    t["input_value"] for t in transitions
                    if t["to_state"] - t["from_state"] == 1
                ]

                if len(threshold_values) > 0:
                    # Use average or first threshold value
                    threshold = sum(threshold_values) / len(threshold_values)

                    return StateMachineTransform(
                        input_column=in_col,
                        output_column=output_col,
                        states=[str(s) for s in states],
                        transitions=[{
                            "type": "threshold",
                            "threshold": threshold,
                            "increment": True,
                        }],
                        initial_state=str(states[0]),
                        params={"mode": "segment_id"},
                    )

    return None


def _detect_neighbor_filter(
    input_rows: List[Dict[str, Any]],
    output_rows: List[Dict[str, Any]],
    kept_indices: List[int],
    output_col: str = None,
) -> Optional[NeighborFilterTransform]:
    """Detect neighbor-based filtering (lookahead/lookbehind patterns)."""
    if not input_rows or not output_rows:
        return None

    all_indices = set(range(len(input_rows)))
    dropped = all_indices - set(kept_indices)

    if not dropped:
        return None

    # Analyze patterns of dropped rows relative to neighbors
    # Check if dropping depends on next/prev row values

    for col in input_rows[0].keys():
        dropped_by_next = True
        dropped_by_prev = True

        for idx in dropped:
            if idx < len(input_rows) - 1:
                next_val = input_rows[idx + 1].get(col)
                if next_val is None:
                    dropped_by_next = False
                    break

            if idx > 0:
                prev_val = input_rows[idx - 1].get(col)
                if prev_val is None:
                    dropped_by_prev = False
                    break

        # Check for drop when next row has certain value
        dropped_next_values = {}
        for idx in dropped:
            if idx < len(input_rows) - 1:
                next_val = input_rows[idx + 1].get(col)
                if next_val:
                    key = str(next_val)
                    dropped_next_values[key] = dropped_next_values.get(key, 0) + 1

        # A column where most dropped rows have same "next" value
        if dropped_next_values:
            most_common_next = max(dropped_next_values.items(), key=lambda x: x[1])
            if most_common_next[1] >= len(dropped) * 0.5:
                return NeighborFilterTransform(
                    condition=f"next.{col} == {repr(most_common_next[0])}",
                    direction="next",
                    params={"column": col, "value": most_common_next[0]},
                )

        # Check for drop when next row status == 'duplicate'
        if col.lower() in ["status", "type", "flag"]:
            dropped_next_status = {}
            for idx in dropped:
                if idx < len(input_rows) - 1:
                    next_val = input_rows[idx + 1].get(col)
                    if next_val:
                        key = str(next_val)
                        dropped_next_status[key] = dropped_next_status.get(key, 0) + 1

            for status_val, count in dropped_next_status.items():
                if count >= len(dropped) * 0.5:
                    return NeighborFilterTransform(
                        condition=f"next.{col} == {repr(status_val)}",
                        direction="next",
                        params={"column": col, "value": status_val, "pattern": "duplicate_next"},
                    )

    return None


def infer_stateful_transforms(
    input_rows: List[Dict[str, Any]],
    output_rows: List[Dict[str, Any]],
    kept_indices: List[int],
) -> Dict[str, Any]:
    """Infer stateful transforms from input/output sample."""
    if not input_rows or not output_rows:
        return {
            "prefix_transforms": [],
            "window_transforms": [],
            "state_machine_transforms": [],
            "neighbor_filter": None,
        }

    output_columns = set(output_rows[0].keys()) if output_rows else set()
    input_columns = set(input_rows[0].keys()) if input_rows else set()

    results = {
        "prefix_transforms": [],
        "window_transforms": [],
        "state_machine_transforms": [],
        "neighbor_filter": None,
    }

    for out_col in output_columns:
        # Try prefix transform first (more general)
        prefix_tf = _detect_prefix_transform(input_rows, output_rows, kept_indices, out_col)
        if prefix_tf:
            results["prefix_transforms"].append(prefix_tf)
            continue

        # Try window transform
        window_tf = _detect_window_transform(input_rows, output_rows, kept_indices, out_col)
        if window_tf:
            results["window_transforms"].append(window_tf)
            continue

        # Try state machine
        sm_tf = _detect_state_machine_transform(input_rows, output_rows, kept_indices, out_col)
        if sm_tf:
            results["state_machine_transforms"].append(sm_tf)

    # Detect neighbor-based filtering
    neighbor_filter = _detect_neighbor_filter(input_rows, output_rows, kept_indices)
    if neighbor_filter:
        results["neighbor_filter"] = neighbor_filter

    return results


# ==============================================================================
# Code Generation
# ==============================================================================


def _render_filter_body(filter_pred: Optional[FilterPredicate]) -> str:
    if filter_pred is None:
        return "    return True"
    condition = filter_pred.condition
    if "==" in condition:
        parts = condition.split("==", 1)
        col_name = parts[0].strip().strip("'\"")
        right = parts[1].strip()
        return f"    return row.get('{col_name}', '') == {right}" if right.isdigit() else f"    return str(row.get('{col_name}', '')) == {right}"
    if "!=" in condition:
        parts = condition.split("!=", 1)
        col_name = parts[0].strip().strip("'\"")
        right = parts[1].strip()
        return f"    return row.get('{col_name}', '') != {right}" if right.isdigit() else f"    return str(row.get('{col_name}', '')) != {right}"
    if "row_index in" in condition:
        match = re.search(r"row_index in \[(.*)\]", condition)
        if match:
            return f"    return {match.group(1)}.__contains__(row_index)"
    return "    return True"


def generate_python_code(
    module_name: str,
    data_type: DataType,
    column_transforms: Dict[str, ColumnTransform],
    filter_pred: Optional[FilterPredicate],
) -> str:
    output_columns = [col for col, tr in column_transforms.items() if tr.transform_type != "drop"]
    transform_lines = [_generate_transform_line(col, tr) for col, tr in column_transforms.items() if tr.transform_type != "drop"]
    filter_code = _render_filter_body(filter_pred)

    transform_lines_str = chr(10).join(transform_lines)
    filter_code_str = filter_code
    output_columns_list = output_columns
    data_type_val = data_type.value

    return f'''"""Auto-generated DynamicPreprocessor module."""

import csv
import hashlib
import json
import os
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Any


def _infer_value_type(value: Any) -> Any:
    if value is None or value == "":
        return None
    lower = str(value).lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return str(value)


def _apply_filter(row: Dict[str, Any], row_index: int) -> bool:
{filter_code_str}


def _apply_transform(row: Dict[str, Any], row_index: int) -> Optional[Dict[str, Any]]:
    output_row = {{{{}}}}{transform_lines_str}
    return output_row


class DynamicPreprocessor:
    def __init__(self, buffer: int, cache_dir: Optional[str] = None):
        self.buffer = buffer
        self.cache_dir = cache_dir
        self._format = '{data_type_val}'
        self._output_columns = {output_columns_list}
        if cache_dir:
            Path(cache_dir).mkdir(parents=True, exist_ok=True)

    def __call__(self, path: str) -> Iterator[Dict[str, Any]]:
        return self._process(path)

    def _get_cache_path(self, input_path: str, use_index: int = -1) -> str:
        key = hashlib.md5(input_path.encode()).hexdigest()
        if use_index >= 0:
            return os.path.join(self.cache_dir, f"{{key}}_row_{{use_index}}.json")
        return os.path.join(self.cache_dir, f"{{key}}_index.txt")

    def _load_from_cache(self, input_path: str) -> set:
        if not self.cache_dir:
            return set()
        cache_path = self._get_cache_path(input_path)
        if not os.path.exists(cache_path):
            return set()
        try:
            with open(cache_path, 'r') as f:
                return {{int(line.strip()) for line in f if line.strip().isdigit()}}
        except Exception:
            return set()

    def _save_to_cache(self, input_path: str, indices: set):
        if not self.cache_dir:
            return
        cache_path = self._get_cache_path(input_path)
        with open(cache_path, 'w') as f:
            for idx in sorted(indices):
                f.write(f"{{idx}}\\n")

    def _process(self, path: str) -> Iterator[Dict[str, Any]]:
        processed_indices = self._load_from_cache(path) if self.cache_dir else set()
        buffer_rows = []
        buffer_indices = []

        def process_buffer():
            results = []
            result_indices = []
            for row, idx in zip(buffer_rows, buffer_indices):
                if _apply_filter(row, idx):
                    result = _apply_transform(row, idx)
                    if result:
                        results.append(result)
                        result_indices.append(idx)
            return results, result_indices

        def yield_from_buffer():
            if buffer_rows:
                results, result_indices = process_buffer()
                for r in results:
                    yield r
                processed_indices.update(result_indices)
                return [], []
            return [], []

        if self._format == 'csv':
            with open(path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for idx, row in enumerate(reader):
                    if idx in processed_indices:
                        continue
                    buffer_rows.append(row)
                    buffer_indices.append(idx)
                    if len(buffer_rows) >= self.buffer:
                        for r, _ in [process_buffer()]:
                            yield r
                        buffer_rows, buffer_indices = [], []
                for r in yield_from_buffer():
                    yield r

        elif self._format == 'tsv':
            with open(path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f, delimiter='\\t')
                for idx, row in enumerate(reader):
                    if idx in processed_indices:
                        continue
                    buffer_rows.append(row)
                    buffer_indices.append(idx)
                    if len(buffer_rows) >= self.buffer:
                        for r, _ in [process_buffer()]:
                            yield r
                        buffer_rows, buffer_indices = [], []
                for r in yield_from_buffer():
                    yield r

        elif self._format == 'jsonl':
            with open(path, 'r', encoding='utf-8') as f:
                for idx, line in enumerate(f):
                    line = line.strip()
                    if not line:
                        continue
                    if idx in processed_indices:
                        continue
                    buffer_rows.append(json.loads(line))
                    buffer_indices.append(idx)
                    if len(buffer_rows) >= self.buffer:
                        for r, _ in [process_buffer()]:
                            yield r
                        buffer_rows, buffer_indices = [], []
                for r in yield_from_buffer():
                    yield r

        elif self._format == 'json':
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if not isinstance(data, list):
                    data = [data]
                for idx, row in enumerate(data):
                    if idx in processed_indices:
                        continue
                    buffer_rows.append(row)
                    buffer_indices.append(idx)
                    if len(buffer_rows) >= self.buffer:
                        for r, _ in [process_buffer()]:
                            yield r
                        buffer_rows, buffer_indices = [], []
                for r in yield_from_buffer():
                    yield r

        if self.cache_dir:
            self._save_to_cache(path, processed_indices)
'''



def generate_javascript_code(
    module_name: str,
    data_type: DataType,
    column_transforms: Dict[str, ColumnTransform],
    filter_pred: Optional[FilterPredicate],
) -> str:
    output_columns = [col for col, tr in column_transforms.items() if tr.transform_type != "drop"]
    transform_lines = [_generate_transform_line_js(col, tr) for col, tr in column_transforms.items() if tr.transform_type != "drop"]
    filter_code = _render_filter_body_js(filter_pred)

    return f'''// Auto-generated DynamicPreprocessor module

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

function inferValueType(value) {{
    if (value === null || value === undefined || value === "") return null;
    if (typeof value === 'boolean') return value;
    if (typeof value === 'string') {{
        const lower = value.toLowerCase();
        if (lower === 'true') return true;
        if (lower === 'false') return false;
    }}
    const intVal = parseInt(value, 10);
    if (!isNaN(intVal) && String(intVal) === String(value).trim()) return intVal;
    const floatVal = parseFloat(value);
    if (!isNaN(floatVal)) return floatVal;
    return String(value);
}}

function applyFilter(row, rowIndex) {{
{filter_code}
}}

function applyTransform(row, rowIndex) {{
    const outputRow = {{}};
{chr(10).join(transform_lines)}
    return outputRow;
}}

class DynamicPreprocessor {{
    constructor(options = {{}}) {{
        this.buffer = options.buffer || 1024;
        this.cacheDir = options.cache_dir || null;
        this.format = '{data_type.value}';
        this.outputColumns = {output_columns};
        if (this.cacheDir && !fs.existsSync(this.cacheDir)) {{
            fs.mkdirSync(this.cacheDir, {{ recursive: true }});
        }}
    }}

    process(path) {{
        return this._process(path);
    }}

    _getCachePath(filePath, index = -1) {{
        const key = crypto.createHash('md5').update(filePath).digest('hex');
        if (index >= 0) return path.join(this.cacheDir, `$\{{key}}_row_\{{index}}.json`);
        return path.join(this.cacheDir, `$\{{key}}_index.txt`);
    }}

    _loadFromCache(filePath) {{
        if (!this.cacheDir) return new Set();
        const cachePath = this._getCachePath(filePath);
        if (!fs.existsSync(cachePath)) return new Set();
        try {{
            const content = fs.readFileSync(cachePath, 'utf-8');
            const indices = new Set();
            for (const line of content.trim().split('\\n')) {{
                const idx = parseInt(line, 10);
                if (!isNaN(idx)) indices.add(idx);
            }}
            return indices;
        }} catch {{
            return new Set();
        }}
    }}

    _saveToCache(filePath, indices) {{
        if (!this.cacheDir) return;
        const cachePath = this._getCachePath(filePath);
        const lines = Array.from(indices).sort((a, b) => a - b).map(idx => String(idx));
        fs.writeFileSync(cachePath, lines.join('\\n'));
    }}

    _process(filePath) {{
        let processedIndices = this._loadFromCache(filePath);
        let bufferRows = [];
        let bufferIndices = [];

        const processBuffer = () => {{
            const results = [];
            const resultIndices = [];
            for (let i = 0; i < bufferRows.length; i++) {{
                const row = bufferRows[i];
                const idx = bufferIndices[i];
                if (applyFilter(row, idx)) {{
                    const result = applyTransform(row, idx);
                    if (result) {{
                        results.push(result);
                        resultIndices.push(idx);
                    }}
                }}
            }}
            return {{ results, resultIndices }};
        }};

        const yieldFromBuffer = () => {{
            if (bufferRows.length === 0) return [];
            const {{ results, resultIndices }} = processBuffer();
            for (const idx of resultIndices) processedIndices.add(idx);
            bufferRows = [];
            bufferIndices = [];
            return results;
        }};

        const fmt = this.format;
        const buffer = this.buffer;

        if (fmt === 'csv') {{
            const content = fs.readFileSync(filePath, 'utf-8');
            const lines = content.trim().split('\\n');
            if (lines.length === 0) return;
            const headers = lines[0].split(',');
            for (let lineNum = 1; lineNum < lines.length; lineNum++) {{
                const idx = lineNum - 1;
                if (processedIndices.has(idx)) continue;
                const values = lines[lineNum].split(',');
                const row = {{}};
                for (let i = 0; i < headers.length; i++) {{
                    row[headers[i].trim()] = values[i] !== undefined ? values[i].trim() : '';
                }}
                bufferRows.push(row);
                bufferIndices.push(idx);
                if (bufferRows.length >= buffer) {{
                    yield* yieldFromBuffer();
                }}
            }}
            yield* yieldFromBuffer();
        }}

        else if (fmt === 'tsv') {{
            const content = fs.readFileSync(filePath, 'utf-8');
            const lines = content.trim().split('\\n');
            if (lines.length === 0) return;
            const headers = lines[0].split('\\t');
            for (let lineNum = 1; lineNum < lines.length; lineNum++) {{
                const idx = lineNum - 1;
                if (processedIndices.has(idx)) continue;
                const values = lines[lineNum].split('\\t');
                const row = {{}};
                for (let i = 0; i < headers.length; i++) {{
                    row[headers[i].trim()] = values[i] !== undefined ? values[i].trim() : '';
                }}
                bufferRows.push(row);
                bufferIndices.push(idx);
                if (bufferRows.length >= buffer) {{
                    yield* yieldFromBuffer();
                }}
            }}
            yield* yieldFromBuffer();
        }}

        else if (fmt === 'jsonl') {{
            const content = fs.readFileSync(filePath, 'utf-8');
            const lines = content.trim().split('\\n');
            for (let lineNum = 0; lineNum < lines.length; lineNum++) {{
                const line = lines[lineNum].trim();
                if (!line) continue;
                const idx = lineNum;
                if (processedIndices.has(idx)) continue;
                let row;
                try {{ row = JSON.parse(line); }} catch {{ continue; }}
                bufferRows.push(row);
                bufferIndices.push(idx);
                if (bufferRows.length >= buffer) {{
                    yield* yieldFromBuffer();
                }}
            }}
            yield* yieldFromBuffer();
        }}

        else if (fmt === 'json') {{
            const content = fs.readFileSync(filePath, 'utf-8');
            let data;
            try {{ data = JSON.parse(content); }} catch {{ return; }}
            if (!Array.isArray(data)) data = [data];
            for (let idx = 0; idx < data.length; idx++) {{
                if (processedIndices.has(idx)) continue;
                const row = data[idx];
                bufferRows.push(row);
                bufferIndices.push(idx);
                if (bufferRows.length >= buffer) {{
                    yield* yieldFromBuffer();
                }}
            }}
            yield* yieldFromBuffer();
        }}

        if (this.cacheDir) {{
            this._saveToCache(filePath, processedIndices);
        }}
    }}
}}

function createPreprocessor(options) {{
    return new DynamicPreprocessor(options);
}}

module.exports = {{
    DynamicPreprocessor,
    createPreprocessor,
    default: createPreprocessor,
}};
'''


# ==============================================================================
# C++ Code Generation
# ==============================================================================


def generate_cpp_code(
    module_name: str,
    data_type: DataType,
    column_transforms: Dict[str, ColumnTransform],
    filter_pred: Optional[FilterPredicate],
    stateful_transforms: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str]:
    """Generate C++ header and implementation files."""
    output_columns = [col for col, tr in column_transforms.items() if tr.transform_type != "drop"]

    # Generate header file
    header_content = generate_cpp_header(module_name, data_type, column_transforms, filter_pred, output_columns, stateful_transforms)

    # Generate implementation file
    impl_content = generate_cpp_implementation(module_name, data_type, column_transforms, filter_pred, output_columns, stateful_transforms)

    return header_content, impl_content


def generate_cpp_header(
    module_name: str,
    data_type: DataType,
    column_transforms: Dict[str, ColumnTransform],
    filter_pred: Optional[FilterPredicate],
    output_columns: List[str],
    stateful_transforms: Optional[Dict[str, Any]] = None,
) -> str:
    """Generate C++ header file."""
    stateful = stateful_transforms or {
        "prefix_transforms": [],
        "window_transforms": [],
        "state_machine_transforms": [],
        "neighbor_filter": None,
    }

    header = f'''#pragma once
#include <map>
#include <string>
#include <optional>
#include <vector>
#include <deque>
#include <fstream>
#include <sstream>
#include <iostream>
#include <memory>
#include <cmath>

namespace {module_name} {{

enum class ValueType {{
    Null,
    Bool,
    Int,
    Double,
    String
}};

struct Value {{
    ValueType type {{ ValueType::Null }};
    bool bool_value {{ false }};
    long long int_value {{ 0 }};
    double double_value {{ 0.0 }};
    std::string string_value;
}};

using Row = std::map<std::string, Value>;

class DynamicPreprocessor {{
public:
    explicit DynamicPreprocessor(std::size_t buffer);
    DynamicPreprocessor(std::size_t buffer, const std::string& cache_dir);
    ~DynamicPreprocessor();

    void open(const std::string& path);
    bool next(Row& out);

private:
    std::size_t buffer_;
    std::string cache_dir_;
    std::string format_;
    std::ifstream file_stream_;
    std::size_t current_row_;
    std::set<std::size_t> processed_indices_;

    // State for stateful transforms
    std::map<std::string, double> prefix_sums_;
    std::map<std::string, std::size_t> prefix_counts_;
    std::map<std::string, std::deque<double>> window_buffers_;
    std::map<std::string, std::string> state_machine_states_;

    std::string get_cache_path(const std::string& input_path) const;
    void load_from_cache(const std::string& input_path);
    void save_to_cache(const std::string& input_path);

    Value parse_value(const std::string& str) const;
    bool apply_filter(std::size_t row_index) const;
    Row apply_transform(const std::map<std::string, std::string>& row, std::size_t row_index);

    void reset_state();
    void update_state(Row& row, std::size_t row_index);

    std::map<std::string, std::string> parse_csv_line(const std::string& line, const std::vector<std::string>& headers);
    std::map<std::string, std::string> parse_tsv_line(const std::string& line, const std::vector<std::string>& headers);
    std::map<std::string, std::string> parse_json_line(const std::string& line);
};

}} // namespace {module_name}
'''
    return header


def generate_cpp_implementation(
    module_name: str,
    data_type: DataType,
    column_transforms: Dict[str, ColumnTransform],
    filter_pred: Optional[FilterPredicate],
    output_columns: List[str],
    stateful_transforms: Optional[Dict[str, Any]] = None,
) -> str:
    """Generate C++ implementation file."""
    stateful = stateful_transforms or {
        "prefix_transforms": [],
        "window_transforms": [],
        "state_machine_transforms": [],
        "neighbor_filter": None,
    }

    format_str = data_type.value

    # Generate transform code
    transform_cases = _generate_cpp_transform_cases(column_transforms, output_columns)

    # Generate filter code
    filter_code = _generate_cpp_filter_code(filter_pred)

    impl = f'''#include "dynamic_preprocessor.h"

namespace {module_name} {{

DynamicPreprocessor::DynamicPreprocessor(std::size_t buffer)
    : buffer_(buffer), cache_dir_(""), current_row_(0)
{{
}}

DynamicPreprocessor::DynamicPreprocessor(std::size_t buffer, const std::string& cache_dir)
    : buffer_(buffer), cache_dir_(cache_dir), current_row_(0)
{{
}}

DynamicPreprocessor::~DynamicPreprocessor()
{{
    if (file_stream_.is_open()) {{
        file_stream_.close();
    }}
}}

void DynamicPreprocessor::open(const std::string& path)
{{
    if (file_stream_.is_open()) {{
        file_stream_.close();
    }}

    file_stream_.open(path);
    if (!file_stream_.is_open()) {{
        throw std::runtime_error("Cannot open file: " + path);
    }}

    current_row_ = 0;
    reset_state();

    if (!cache_dir_.empty()) {{
        load_from_cache(path);
    }}
}}

bool DynamicPreprocessor::next(Row& out)
{{
    if (!file_stream_.is_open()) {{
        return false;
    }}

    std::string line;
    std::vector<std::string> headers;

    // Read header if needed
    if (current_row_ == 0 && format_ == "csv") {{
        if (std::getline(file_stream_, line)) {{
            std::istringstream ss(line);
            std::string cell;
            while (std::getline(ss, cell, ',')) {{
                headers.push_back(cell);
            }}
        }}
    }}

    while (std::getline(file_stream_, line)) {{
        if (line.empty()) continue;

        // Skip processed rows
        if (processed_indices_.count(current_row_)) {{
            current_row_++;
            continue;
        }}

        std::map<std::string, std::string> raw_row;

        if (format_ == "csv") {{
            raw_row = parse_csv_line(line, headers);
        }} else if (format_ == "tsv") {{
            raw_row = parse_tsv_line(line, headers);
        }} else if (format_ == "jsonl") {{
            raw_row = parse_json_line(line);
        }} else if (format_ == "json") {{
            // For JSON array format, we need special handling
            // Simplified: treat as JSONL
            raw_row = parse_json_line(line);
        }}

        std::size_t row_idx = current_row_;
        current_row_++;

        if (apply_filter(row_idx)) {{
            out = apply_transform(raw_row, row_idx);
            update_state(out, row_idx);
            return true;
        }}
    }}

    return false;
}}

std::string DynamicPreprocessor::get_cache_path(const std::string& input_path) const
{{
    // Simple hash-based cache path
    std::string key = std::to_string(std::hash<std::string>{{}}(input_path));
    if (cache_dir_.empty()) {{
        return "";
    }}
    return cache_dir_ + "/" + key + "_index.txt";
}}

void DynamicPreprocessor::load_from_cache(const std::string& input_path)
{{
    std::string cache_path = get_cache_path(input_path);
    if (cache_path.empty()) return;

    std::ifstream cache_file(cache_path);
    if (!cache_file.is_open()) return;

    std::string line;
    while (std::getline(cache_file, line)) {{
        try {{
            std::size_t idx = std::stoull(line);
            processed_indices_.insert(idx);
        }} catch (...) {{
            continue;
        }}
    }}

    cache_file.close();
}}

void DynamicPreprocessor::save_to_cache(const std::string& input_path)
{{
    std::string cache_path = get_cache_path(input_path);
    if (cache_path.empty()) return;

    std::ofstream cache_file(cache_path);
    if (!cache_file.is_open()) return;

    for (auto idx : processed_indices_) {{
        cache_file << idx << "\\n";
    }}

    cache_file.close();
}}

Value DynamicPreprocessor::parse_value(const std::string& str) const
{{
    Value val;

    if (str.empty()) {{
        val.type = ValueType::Null;
        return val;
    }}

    // Check boolean
    std::string lower = str;
    std::transform(lower.begin(), lower.end(), lower.begin(), ::tolower);
    if (lower == "true") {{
        val.type = ValueType::Bool;
        val.bool_value = true;
        return val;
    }}
    if (lower == "false") {{
        val.type = ValueType::Bool;
        val.bool_value = false;
        return val;
    }}

    // Check integer
    try {{
        size_t pos;
        long long int_val = std::stoll(str, &pos);
        if (pos == str.length()) {{
            val.type = ValueType::Int;
            val.int_value = int_val;
            return val;
        }}
    }} catch (...) {{
    }}

    // Check double
    try {{
        size_t pos;
        double double_val = std::stod(str, &pos);
        if (pos == str.length()) {{
            val.type = ValueType::Double;
            val.double_value = double_val;
            return val;
        }}
    }} catch (...) {{
    }}

    // Default to string
    val.type = ValueType::String;
    val.string_value = str;
    return val;
}}

bool DynamicPreprocessor::apply_filter(std::size_t row_index) const
{filter_code}
}}

Row DynamicPreprocessor::apply_transform(const std::map<std::string, std::string>& row, std::size_t row_index)
{{
    Row output;
{transform_cases}
    return output;
}}

void DynamicPreprocessor::reset_state()
{{
    prefix_sums_.clear();
    prefix_counts_.clear();
    window_buffers_.clear();
    state_machine_states_.clear();
    processed_indices_.clear();
}}

void DynamicPreprocessor::update_state(Row& row, std::size_t row_index)
{{
    // Update stateful transforms based on inferred types
{generate_cpp_stateful_update()}
}}

std::map<std::string, std::string> DynamicPreprocessor::parse_csv_line(const std::string& line, const std::vector<std::string>& headers)
{{
    std::map<std::string, std::string> result;
    std::istringstream ss(line);
    std::string cell;
    std::size_t col_idx = 0;

    while (std::getline(ss, cell, ',')) {{
        if (col_idx < headers.size()) {{
            result[headers[col_idx]] = cell;
        }}
        col_idx++;
    }}

    return result;
}}

std::map<std::string, std::string> DynamicPreprocessor::parse_tsv_line(const std::string& line, const std::vector<std::string>& headers)
{{
    std::map<std::string, std::string> result;
    std::istringstream ss(line);
    std::string cell;
    std::size_t col_idx = 0;

    while (std::getline(ss, cell, '\\t')) {{
        if (col_idx < headers.size()) {{
            result[headers[col_idx]] = cell;
        }}
        col_idx++;
    }}

    return result;
}}

std::map<std::string, std::string> DynamicPreprocessor::parse_json_line(const std::string& line)
{{
    std::map<std::string, std::string> result;

    // Very simple JSON parsing for flat objects
    std::string content = line;
    // Remove outer braces
    if (content.front() == '{{' && content.back() == '}}') {{
        content = content.substr(1, content.length() - 2);
    }}

    std::istringstream ss(content);
    std::string pair;

    while (std::getline(ss, pair, ',')) {{
        std::size_t colon_pos = pair.find(':');
        if (colon_pos == std::string::npos) continue;

        std::string key = pair.substr(0, colon_pos);
        std::string value = pair.substr(colon_pos + 1);

        // Trim whitespace and quotes
        key.erase(0, key.find_first_not_of(" \\t\\n\\r\"'"));
        key.erase(key.find_last_not_of(" \\t\\n\\r\"'") + 1);
        value.erase(0, value.find_first_not_of(" \\t\\n\\r\"'"));
        value.erase(value.find_last_not_of(" \\t\\n\\r\"'") + 1);

        result[key] = value;
    }}

    return result;
}}

}} // namespace {module_name}
'''
    return impl


def _generate_cpp_transform_cases(column_transforms: Dict[str, ColumnTransform], output_columns: List[str]) -> str:
    """Generate C++ transform case statements."""
    cases = []

    for col in output_columns:
        transform = column_transforms[col]

        if transform.transform_type == "identity" or transform.transform_type == "copy":
            cases.append(f"    output[\"{col}\"] = parse_value(row.at(\"{transform.input_column}\"));")
        elif transform.transform_type == "const":
            val = transform.params.get("value", "")
            if val is True:
                val_str = "true"
            elif val is False:
                val_str = "false"
            elif val is None:
                val_str = ""
            elif isinstance(val, str):
                val_str = f'"{val}"'
            else:
                val_str = str(val)
            cases.append(f'    output["{col}"] = parse_value("{val_str}");')
        elif transform.transform_type == "numeric":
            a = transform.params.get("a", 1)
            b = transform.params.get("b", 0)
            in_col = transform.input_column
            cases.append(f'''    output["{col}"] = parse_value(std::to_string((std::stod(row.at("{in_col}")) * {a}) + {b}));''')
        elif transform.transform_type == "string_lower":
            in_col = transform.input_column
            cases.append(f'''    {{
        std::string temp = row.at("{in_col}");
        std::transform(temp.begin(), temp.end(), temp.begin(), ::tolower);
        output["{col}"] = parse_value(temp);
    }}''')
        elif transform.transform_type == "string_upper":
            in_col = transform.input_column
            cases.append(f'''    {{
        std::string temp = row.at("{in_col}");
        std::transform(temp.begin(), temp.end(), temp.begin(), ::toupper);
        output["{col}"] = parse_value(temp);
    }}''')
        elif transform.transform_type == "string_trim":
            in_col = transform.input_column
            cases.append(f'''    {{
        std::string temp = row.at("{in_col}");
        temp.erase(0, temp.find_first_not_of(" \\t\\n\\r"));
        temp.erase(temp.find_last_not_of(" \\t\\n\\r") + 1);
        output["{col}"] = parse_value(temp);
    }}''')
        elif transform.transform_type == "string_concat":
            cols = transform.params.get("columns", [])
            concat_expr = " + ".join(f'row.at("{c}")' for c in cols)
            cases.append(f'    output["{col}"] = parse_value({concat_expr});')
        else:
            cases.append(f'    output["{col}"] = parse_value(row.at("{col}"));')

    return "\\n".join(cases)


def _generate_cpp_filter_code(filter_pred: Optional[FilterPredicate]) -> str:
    """Generate C++ filter code."""
    if filter_pred is None:
        return "    return true;"

    condition = filter_pred.condition

    if "==" in condition:
        parts = condition.split("==", 1)
        col_name = parts[0].strip().strip("'\"")
        right = parts[1].strip().strip("'\"")
        if right.isdigit():
            return f'    return true;  // TODO: implement filter for {condition}'
        else:
            return f'    return true;  // TODO: implement filter for {condition}'
    elif "!=" in condition:
        parts = condition.split("!=", 1)
        col_name = parts[0].strip().strip("'\"")
        right = parts[1].strip().strip("'\"")
        return f'    return true;  // TODO: implement filter for {condition}'
    elif "row_index in" in condition:
        match = re.search(r"row_index in \[(.*)\]", condition)
        if match:
            return f"    return true;  // TODO: implement filter for {condition}"

    return "    return true;  // No filter implementation for this condition"


def generate_cpp_stateful_update() -> str:
    """Generate state update code for C++."""
    return '''    // Stateful transforms are handled in the transform logic
    // This is a placeholder for state update logic
'''


# ==============================================================================
# Rust Code Generation
# ==============================================================================


def generate_rust_code(
    module_name: str,
    data_type: DataType,
    column_transforms: Dict[str, ColumnTransform],
    filter_pred: Optional[FilterPredicate],
    stateful_transforms: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str]:
    """Generate Rust Cargo.toml and lib.rs files."""
    toml_content = generate_rust_cargo_toml(module_name)
    lib_content = generate_rust_lib_rs(module_name, data_type, column_transforms, filter_pred, stateful_transforms)

    return toml_content, lib_content


def generate_rust_cargo_toml(module_name: str) -> str:
    """Generate Rust Cargo.toml."""
    return f'''[package]
name = "{module_name}"
version = "0.1.0"
edition = "2021"

[lib]
name = "{module_name}"
path = "src/lib.rs"

[dependencies]
serde = {{ version = "1.0", features = ["derive"] }}
serde_json = "1.0"
'''


def generate_rust_lib_rs(
    module_name: str,
    data_type: DataType,
    column_transforms: Dict[str, ColumnTransform],
    filter_pred: Optional[FilterPredicate],
    stateful_transforms: Optional[Dict[str, Any]] = None,
) -> str:
    """Generate Rust library source."""
    stateful = stateful_transforms or {
        "prefix_transforms": [],
        "window_transforms": [],
        "state_machine_transforms": [],
        "neighbor_filter": None,
    }

    format_str = data_type.value

    # Generate use statements and helpers
    use_statements = """use serde::Deserialize;
use std::collections::{BTreeMap, VecDeque};
use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::Path;
"""

    # Generate Value enum and Row type
    value_type = """
pub #[derive(Clone, Debug, PartialEq)]
pub enum Value {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Str(String),
}

pub type Row = BTreeMap<String, Value>;
"""

    # Generate filter code
    filter_code = _generate_rust_filter_code(filter_pred)

    # Generate transform code
    transform_code = _generate_rust_transform_code(column_transforms)

    impl_block = f'''
pub struct DynamicPreprocessor {{
    buffer: usize,
    cache_dir: Option<String>,
    format: String,
    current_row: usize,
    processed_indices: std::collections::HashSet<usize>,

    // State for stateful transforms
    prefix_sums: std::collections::HashMap<String, f64>,
    prefix_counts: std::collections::HashMap<String, usize>,
    window_buffers: std::collections::HashMap<String, VecDeque<f64>>,
    state_machine_states: std::collections::HashMap<String, String>,

    file_path: Option<String>,
}}

impl DynamicPreprocessor {{
    pub fn new(buffer: usize, cache_dir: Option<&str>) -> DynamicPreprocessor {{
        DynamicPreprocessor {{
            buffer,
            cache_dir: cache_dir.map(|s| s.to_string()),
            format: "{format_str}".to_string(),
            current_row: 0,
            processed_indices: std::collections::HashSet::new(),
            prefix_sums: std::collections::HashMap::new(),
            prefix_counts: std::collections::HashMap::new(),
            window_buffers: std::collections::HashMap::new(),
            state_machine_states: std::collections::HashMap::new(),
            file_path: None,
        }}
    }}

    pub fn open<P: AsRef<Path>>(&mut self, path: P) -> std::io::Result<()> {{
        self.file_path = Some(path.as_ref().to_string_lossy().to_string());
        self.current_row = 0;
        self.processed_indices.clear();
        self.prefix_sums.clear();
        self.prefix_counts.clear();
        self.window_buffers.clear();
        self.state_machine_states.clear();

        if let Some(cache_dir) = &self.cache_dir {{
            self.load_from_cache(path.as_ref().to_string_lossy().as_ref());
        }}

        Ok(())
    }}

    fn parse_value(&self, s: &str) -> Value {{
        if s.is_empty() {{
            return Value::Null;
        }}

        let lower = s.to_lowercase();
        if &lower == "true" {{
            return Value::Bool(true);
        }}
        if &lower == "false" {{
            return Value::Bool(false);
        }}

        if let Ok(int_val) = s.parse::<i64>() {{
            return Value::Int(int_val);
        }}

        if let Ok(float_val) = s.parse::<f64>() {{
            return Value::Float(float_val);
        }}

        Value::Str(s.to_string())
    }}

{filter_code}

    fn apply_transform(&mut self, row: &BTreeMap<String, String>, row_index: usize) -> Row {{
        let mut output = Row::new();
{transform_code}
        self.update_state(&output, row_index);
        output
    }}

    fn update_state(&mut self, row: &Row, row_index: usize) {{
        // Stateful transforms update logic
{generate_rust_stateful_update()}
    }}

    fn load_from_cache(&mut self, path: &str) {{
        if let Some(cache_dir) = &self.cache_dir {{
            let cache_path = format!("{{}}/{{}}_index.txt", cache_dir, md5::compute(path));
            if let Ok(content) = std::fs::read_to_string(&cache_path) {{
                for line in content.lines() {{
                    if let Ok(idx) = line.parse::<usize>() {{
                        self.processed_indices.insert(idx);
                    }}
                }}
            }}
        }}
    }}

    fn save_to_cache(&self) {{
        if let Some(cache_dir) = &self.cache_dir {{
            if let Some(path) = &self.file_path {{
                let cache_path = format!("{{}}/{{}}_index.txt", cache_dir, md5::compute(path));
                let mut lines = Vec::new();
                for idx in &self.processed_indices {{
                    lines.push(idx.to_string());
                }}
                lines.sort();
                let _ = std::fs::write(cache_path, lines.join("\\n"));
            }}
        }}
    }}
}}

impl Iterator for DynamicPreprocessor {{
    type Item = Row;

    fn next(&mut self) -> Option<Self::Item> {{
        // This is a simplified implementation
        // In a full implementation, this would read from the actual file
        None
    }}
}}

pub mod api {{
    pub use super::{DynamicPreprocessor, Row, Value};
}}
'''

    # Combine all parts
    rust_code = f"""{use_statements}

{value_type}

{impl_block}
"""
    return rust_code


def _generate_rust_filter_code(filter_pred: Optional[FilterPredicate]) -> str:
    """Generate Rust filter code."""
    if filter_pred is None:
        return """    fn apply_filter(&self, _row_index: usize) -> bool {
        true
    }"""

    condition = filter_pred.condition

    if "==" in condition:
        parts = condition.split("==", 1)
        col_name = parts[0].strip().strip("'\"")
        right = parts[1].strip().strip("'\"")
        if right.isdigit():
            return f"""    fn apply_filter(&self, _row_index: usize) -> bool {{
        // TODO: implement filter for {condition}
        true
    }}"""
        else:
            return f"""    fn apply_filter(&self, _row_index: usize) -> bool {{
        // TODO: implement filter for {condition}
        true
    }}"""
    elif "!=" in condition:
        parts = condition.split("!=", 1)
        col_name = parts[0].strip().strip("'\"")
        right = parts[1].strip().strip("'\"")
        return f"""    fn apply_filter(&self, _row_index: usize) -> bool {{
        // TODO: implement filter for {condition}
        true
    }}"""
    elif "row_index in" in condition:
        match = re.search(r"row_index in \[(.*)\]", condition)
        if match:
            return f"""    fn apply_filter(&self, row_index: usize) -> bool {{
        // TODO: implement filter for {condition}
        true
    }}"""

    return """    fn apply_filter(&self, _row_index: usize) -> bool {
        true
    }"""


def _generate_rust_transform_code(column_transforms: Dict[str, ColumnTransform]) -> str:
    """Generate Rust transform code."""
    lines = []

    for col, transform in column_transforms.items():
        if transform.transform_type == "drop":
            continue

        if transform.transform_type in ("identity", "copy"):
            in_col = transform.input_column
            lines.append(f"        output.insert(\"{col}\".to_string(), self.parse_value(row.get(\"{in_col}\").unwrap_or(&\"\".to_string())));")

        elif transform.transform_type == "const":
            val = transform.params.get("value", "")
            if val is True:
                val_str = "true"
            elif val is False:
                val_str = "false"
            elif val is None:
                val_str = ""
            elif isinstance(val, str):
                val_str = f'"{val}"'
            else:
                val_str = str(val)
            lines.append(f'        output.insert("{col}".to_string(), self.parse_value("{val_str}"));')

        elif transform.transform_type == "numeric":
            a = transform.params.get("a", 1)
            b = transform.params.get("b", 0)
            in_col = transform.input_column
            lines.append(f"""        {{
            let val = row.get("{in_col}").unwrap_or(&"0".to_string()).parse::<f64>().unwrap_or(0.0);
            output.insert("{col}".to_string(), Value::Float(val * {a} + {b}));
        }}""")

        elif transform.transform_type == "string_lower":
            in_col = transform.input_column
            lines.append(f"""        {{
            let val = row.get("{in_col}").unwrap_or(&"".to_string()).to_lowercase();
            output.insert("{col}".to_string(), Value::Str(val));
        }}""")

        elif transform.transform_type == "string_upper":
            in_col = transform.input_column
            lines.append(f"""        {{
            let val = row.get("{in_col}").unwrap_or(&"".to_string()).to_uppercase();
            output.insert("{col}".to_string(), Value::Str(val));
        }}""")

        elif transform.transform_type == "string_trim":
            in_col = transform.input_column
            lines.append(f"""        {{
            let val = row.get("{in_col}").unwrap_or(&"".to_string()).trim().to_string();
            output.insert("{col}".to_string(), Value::Str(val));
        }}""")

        elif transform.transform_type == "string_concat":
            cols = transform.params.get("columns", [])
            concat_expr = " + ".join(f'row.get("{c}").unwrap_or(&"".to_string())' for c in cols)
            lines.append(f"        output.insert(\"{col}\".to_string(), Value::Str({concat_expr}));")

        else:
            lines.append(f'        output.insert("{col}".to_string(), self.parse_value(row.get("{col}").unwrap_or(&"".to_string())));')

    return "\\n".join(lines)


def generate_rust_stateful_update() -> str:
    """Generate Rust state update code."""
    return """        // Stateful transforms update logic goes here
        // This is a placeholder - in a full implementation this would
        // handle prefix sums, window averages, state machines, etc.
"""


# ==============================================================================
# Main CLI
# ==============================================================================


def _generate_transform_line_js(col: str, transform: ColumnTransform) -> str:
    if transform.transform_type == "identity":
        return f"    outputRow['{col}'] = inferValueType(row['{col}']);"
    if transform.transform_type == "copy":
        return f"    outputRow['{col}'] = inferValueType(row['{col}']);"
    if transform.transform_type == "const":
        val = transform.params.get("value", "")
        if val is True:
            val_str = "true"
        elif val is False:
            val_str = "false"
        elif val is None:
            val_str = "null"
        elif isinstance(val, str):
            val_str = f"'{val}'"
        else:
            val_str = str(val)
        return f"    outputRow['{col}'] = {val_str};"
    if transform.transform_type == "numeric":
        a = transform.params.get("a", 1)
        b = transform.params.get("b", 0)
        return f"    outputRow['{col}'] = (Number(row['{transform.input_column}'] || 0) * {a}) + {b};"
    if transform.transform_type == "string_lower":
        return f"    outputRow['{col}'] = String(row['{transform.input_column}'] || '').toLowerCase();"
    if transform.transform_type == "string_upper":
        return f"    outputRow['{col}'] = String(row['{transform.input_column}'] || '').toUpperCase();"
    if transform.transform_type == "string_trim":
        return f"    outputRow['{col}'] = String(row['{transform.input_column}'] || '').trim();"
    if transform.transform_type == "string_concat":
        cols = transform.params.get("columns", [])
        concat_expr = " + ".join(f"String(row['{c}'] || '')" for c in cols)
        return f"    outputRow['{col}'] = {concat_expr};"
    return f"    outputRow['{col}'] = inferValueType(row['{col}']);"


def _render_filter_body_js(filter_pred: Optional[FilterPredicate]) -> str:
    if filter_pred is None:
        return "    return true;"
    condition = filter_pred.condition
    if "==" in condition:
        parts = condition.split("==", 1)
        col_name = parts[0].strip().strip("'\"")
        right = parts[1].strip()
        return f"    return row['{col_name}'] == {right};" if right.isdigit() else f"    return String(row['{col_name}'] || '') == {right};"
    if "!=" in condition:
        parts = condition.split("!=", 1)
        col_name = parts[0].strip().strip("'\"")
        right = parts[1].strip()
        return f"    return row['{col_name}'] != {right};" if right.isdigit() else f"    return String(row['{col_name}'] || '') != {right};"
    if "row_index in" in condition:
        match = re.search(r"row_index in \[(.*)\]", condition)
        if match:
            return f"    return [{match.group(1)}].includes(rowIndex);"
    return "    return true;"


# ==============================================================================
# Main CLI
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(description='Generate a DynamicPreprocessor module from sample input/output.')
    parser.add_argument('module_name', help='Name of the generated module/package')
    parser.add_argument('--output', required=True, help='Output directory')
    parser.add_argument('--sample', required=True, help='Sample directory containing input.{ext} and output.{ext}')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--python', action='store_true', default=True, help='Generate Python module (default)')
    group.add_argument('--javascript', action='store_true', dest='javascript', help='Generate JavaScript module')
    group.add_argument('--cpp', action='store_true', dest='cpp', help='Generate C++ module')
    group.add_argument('--rust', action='store_true', dest='rust', help='Generate Rust module')

    args = parser.parse_args()

    if args.javascript:
        target_lang = 'javascript'
    elif args.cpp:
        target_lang = 'cpp'
    elif args.rust:
        target_lang = 'rust'
    else:
        target_lang = 'python'

    sample_path = Path(args.sample)
    if not sample_path.exists():
        print(f"Error: Sample directory '{args.sample}' does not exist", file=sys.stderr)
        sys.exit(1)

    input_file = output_file = None
    for f in sample_path.iterdir():
        if f.is_file():
            if f.name.startswith('input.'):
                input_file = f
            elif f.name.startswith('output.'):
                output_file = f

    if not input_file or not output_file:
        print("Error: Sample directory must contain 'input.{ext}' and 'output.{ext}'", file=sys.stderr)
        sys.exit(1)

    if input_file.suffix != output_file.suffix:
        print("Error: input and output files must have the same extension", file=sys.stderr)
        sys.exit(1)

    print(f"Loading input from: {input_file}")
    print(f"Loading output from: {output_file}")

    input_data = load_input_data(str(input_file))
    output_data = load_input_data(str(output_file))

    print(f"Loaded {len(input_data)} input rows")
    print(f"Loaded {len(output_data)} output rows")

    print("Inferring transformations...")
    column_transforms, filter_pred = infer_transforms(input_data, output_data)

    print(f"Found {len(column_transforms)} column transformations")
    if filter_pred:
        print(f"Found filter predicate: {filter_pred.condition}")
    else:
        print("No filtering applied")

    data_type = detect_data_format(str(input_file))
    print(f"Generating {target_lang} module...")

    if target_lang == 'python':
        code = generate_python_code(args.module_name, data_type, column_transforms, filter_pred)
        output_path = Path(args.output) / args.module_name
        output_path.mkdir(parents=True, exist_ok=True)

        init_content = f'''"""Auto-generated package: {args.module_name}"""

from .dynamic_preprocessor import DynamicPreprocessor

__all__ = ['DynamicPreprocessor']
'''
        (output_path / '__init__.py').write_text(init_content, encoding='utf-8')
        (output_path / 'dynamic_preprocessor.py').write_text(code, encoding='utf-8')
        print(f"Generated Python package at: {output_path}")
    elif target_lang == 'javascript':
        code = generate_javascript_code(args.module_name, data_type, column_transforms, filter_pred)
        output_path = Path(args.output)
        output_path.mkdir(parents=True, exist_ok=True)
        module_file = output_path / f"{args.module_name}.js"
        module_file.write_text(code, encoding='utf-8')
        print(f"Generated JavaScript module at: {module_file}")
    elif target_lang == 'cpp':
        header_content, impl_content = generate_cpp_code(args.module_name, data_type, column_transforms, filter_pred)
        output_path = Path(args.output) / args.module_name
        output_path.mkdir(parents=True, exist_ok=True)

        (output_path / 'dynamic_preprocessor.h').write_text(header_content, encoding='utf-8')
        (output_path / 'dynamic_preprocessor.cpp').write_text(impl_content, encoding='utf-8')
        print(f"Generated C++ module at: {output_path}")
    elif target_lang == 'rust':
        toml_content, lib_content = generate_rust_code(args.module_name, data_type, column_transforms, filter_pred)
        output_path = Path(args.output) / args.module_name
        src_dir = output_path / 'src'
        output_path.mkdir(parents=True, exist_ok=True)
        src_dir.mkdir(parents=True, exist_ok=True)

        (output_path / 'Cargo.toml').write_text(toml_content, encoding='utf-8')
        (src_dir / 'lib.rs').write_text(lib_content, encoding='utf-8')
        print(f"Generated Rust crate at: {output_path}")

    print("Done!")


if __name__ == '__main__':
    main()
