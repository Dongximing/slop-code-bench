#!/usr/bin/env python3
"""Dynamic Buffer Code Generator with Stateful Transforms (Part 2).

Given a sample input/output pair, infers transformations and generates a
DynamicPreprocessor module that supports stateful, neighborhood-dependent logic.
"""

import argparse
import csv
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Any, Tuple


# ==============================================================================
# Data Models
# ==============================================================================

class DataType(Enum):
    CSV = "csv"
    TSV = "tsv"
    JSONL = "jsonl"
    JSON = "json"


@dataclass
class ColumnTransform:
    """Represents a column transformation (Part 1)."""
    input_column: Optional[str]
    transform_type: str
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FilterPredicate:
    """Represents a filter predicate (Part 1)."""
    condition: str


@dataclass
class PrefixTransform:
    """Cumulative aggregation over all previous rows."""
    input_column: str
    output_column: str
    transform_type: str  # "sum", "avg"
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class WindowTransform:
    """Sliding window aggregation over last W rows."""
    input_column: str
    output_column: str
    window_size: int  # 1-64
    transform_type: str  # "avg"
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class StateMachineTransform:
    """Finite state machine for sequence labeling."""
    input_column: str
    output_column: str
    threshold: float
    initial_state: int = 1
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class NeighborFilter:
    """Filter based on neighboring rows."""
    column: str
    value: Any
    pattern: str  # "duplicate_next"


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


# ==============================================================================
# Transformation Inference
# ==============================================================================

def infer_transforms(
    input_rows: List[Dict[str, Any]],
    output_rows: List[Dict[str, Any]],
) -> Tuple[Dict[str, ColumnTransform], Optional[FilterPredicate],
           List[PrefixTransform], List[WindowTransform], List[StateMachineTransform], Optional[NeighborFilter]]:
    if not input_rows:
        return {}, FilterPredicate("False"), [], [], [], None

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
    filter_pred = _build_filter_predicate(input_rows, kept_indices)

    column_transforms: Dict[str, ColumnTransform] = {}
    prefix_transforms: List[PrefixTransform] = []
    window_transforms: List[WindowTransform] = []
    state_machine_transforms: List[StateMachineTransform] = []
    neighbor_filter: Optional[NeighborFilter] = None

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

    # Detect stateful transforms
    prefix_transforms, window_transforms, state_machine_transforms, neighbor_filter = \
        detect_stateful_transforms(input_rows, output_rows, list(kept_indices))

    return column_transforms, filter_pred, prefix_transforms, window_transforms, state_machine_transforms, neighbor_filter


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
# Stateful Transform Detection (Part 2)
# ==============================================================================

def detect_stateful_transforms(
    input_rows: List[Dict[str, Any]],
    output_rows: List[Dict[str, Any]],
    kept_indices: List[int],
) -> Tuple[List[PrefixTransform], List[WindowTransform], List[StateMachineTransform], Optional[NeighborFilter]]:
    """Detect stateful transforms from input/output sample."""
    if not input_rows or not output_rows:
        return [], [], [], None

    prefix_transforms = []
    window_transforms = []
    state_machine_transforms = []
    neighbor_filter = None

    output_columns = set(output_rows[0].keys()) if output_rows else set()
    input_columns = set(input_rows[0].keys()) if input_rows else set()

    for out_col in output_columns:
        # Try prefix transforms first
        prefix_tf = _detect_prefix_transform(input_rows, output_rows, kept_indices, out_col)
        if prefix_tf:
            prefix_transforms.append(prefix_tf)
            continue

        # Try window transforms
        window_tf = _detect_window_transform(input_rows, output_rows, kept_indices, out_col)
        if window_tf:
            window_transforms.append(window_tf)
            continue

        # Try state machine
        sm_tf = _detect_state_machine_transform(input_rows, output_rows, kept_indices, out_col)
        if sm_tf:
            state_machine_transforms.append(sm_tf)

    # Detect neighbor-based filtering
    neighbor_filter = _detect_neighbor_filter(input_rows, output_rows, kept_indices)

    return prefix_transforms, window_transforms, state_machine_transforms, neighbor_filter


def _detect_prefix_transform(
    input_rows: List[Dict[str, Any]],
    output_rows: List[Dict[str, Any]],
    kept_indices: List[int],
    output_col: str,
) -> Optional[PrefixTransform]:
    """Detect prefix/cumulative transforms like running sum, average."""
    if not input_rows or not output_rows:
        return None

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
    """Detect sliding window transforms (running average over last W rows)."""
    if not input_rows or not output_rows:
        return None

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
            # Check for window average
            avg_match = True
            for i, ov in enumerate(out_vals):
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

    return None


def _detect_state_machine_transform(
    input_rows: List[Dict[str, Any]],
    output_rows: List[Dict[str, Any]],
    kept_indices: List[int],
    output_col: str,
) -> Optional[StateMachineTransform]:
    """Detect finite state machine for sequence labeling (segment IDs)."""
    if not input_rows or not output_rows:
        return None

    out_vals = [output_rows[i].get(output_col) for i in range(len(output_rows))]

    # Output must be integer states
    try:
        out_ints = [int(v) for v in out_vals if v is not None]
        if len(out_ints) < 2:
            return None
    except (ValueError, TypeError):
        return None

    # Look for pattern where state changes based on threshold in input column
    for in_col in input_rows[0].keys():
        in_vals_numeric = []
        for i in kept_indices:
            try:
                val = float(input_rows[i].get(in_col, 0))
                in_vals_numeric.append(val)
            except (ValueError, TypeError):
                in_vals_numeric.append(None)

        if len(in_vals_numeric) < 2:
            continue

        # Find threshold: value where state increments
        transitions = []
        for i in range(1, len(out_ints)):
            prev_state = out_ints[i-1]
            curr_state = out_ints[i]
            in_val = in_vals_numeric[i]

            if in_val is not None and curr_state != prev_state:
                transitions.append({
                    "from_state": prev_state,
                    "to_state": curr_state,
                    "input_value": in_val,
                })

        if len(transitions) > 0:
            # Check if all state increments are by 1
            all_increments_by_1 = True
            for i in range(1, len(out_ints)):
                diff = out_ints[i] - out_ints[i-1]
                if diff != 0 and diff != 1:
                    all_increments_by_1 = False
                    break

            if all_increments_by_1:
                # Find threshold from transitions
                threshold_values = [t["input_value"] for t in transitions
                                   if t["to_state"] - t["from_state"] == 1]

                if threshold_values:
                    threshold = sum(threshold_values) / len(threshold_values)

                    return StateMachineTransform(
                        input_column=in_col,
                        output_column=output_col,
                        threshold=threshold,
                        initial_state=out_ints[0],
                        params={"mode": "segment_id"},
                    )

    return None


def _detect_neighbor_filter(
    input_rows: List[Dict[str, Any]],
    output_rows: List[Dict[str, Any]],
    kept_indices: List[int],
) -> Optional[NeighborFilter]:
    """Detect neighbor-based filtering (drop if next row has certain value)."""
    if not input_rows or not output_rows:
        return None

    all_indices = set(range(len(input_rows)))
    dropped = all_indices - set(kept_indices)

    if not dropped:
        return None

    # Check for pattern: drop row i if row i+1 has status == 'duplicate'
    for col in input_rows[0].keys():
        dropped_next_values = {}
        for idx in dropped:
            if idx < len(input_rows) - 1:
                next_val = input_rows[idx + 1].get(col)
                if next_val:
                    key = str(next_val)
                    dropped_next_values[key] = dropped_next_values.get(key, 0) + 1

        if dropped_next_values:
            most_common_next = max(dropped_next_values.items(), key=lambda x: x[1])
            if most_common_next[1] >= len(dropped) * 0.5:
                return NeighborFilter(
                    column=col,
                    value=most_common_next[0],
                    pattern="duplicate_next",
                )

    return None


# ==============================================================================
# Code Generation - Base Template (Part 1)
# ==============================================================================

def _generate_transform_line(col: str, transform: ColumnTransform) -> str:
    if transform.transform_type == "identity":
        return f"    output_row['{col}'] = _infer_value_type(str(row.get('{col}', '')))"
    if transform.transform_type == "copy":
        return f"    output_row['{col}'] = _infer_value_type(str(row.get('{col}', '')))"
    if transform.transform_type == "const":
        val = transform.params.get("value", "")
        if val is True:
            val_str = "True"
        elif val is False:
            val_str = "False"
        elif val is None:
            val_str = "None"
        elif isinstance(val, str):
            val_str = f'"{val}"'
        else:
            val_str = str(val)
        return f"    output_row['{col}'] = {val_str}"
    if transform.transform_type == "numeric":
        a = transform.params.get("a", 1)
        b = transform.params.get("b", 0)
        return f"    output_row['{col}'] = (float(row.get('{transform.input_column}', 0)) * {a}) + {b}"
    if transform.transform_type == "string_lower":
        return f"    output_row['{col}'] = str(row.get('{transform.input_column}', '')).lower()"
    if transform.transform_type == "string_upper":
        return f"    output_row['{col}'] = str(row.get('{transform.input_column}', '')).upper()"
    if transform.transform_type == "string_trim":
        return f"    output_row['{col}'] = str(row.get('{transform.input_column}', '')).strip()"
    if transform.transform_type == "string_concat":
        cols = transform.params.get("columns", [])
        concat_expr = " + ".join(f"str(row.get('{c}', ''))" for c in cols)
        return f"    output_row['{col}'] = {concat_expr}"
    return f"    output_row['{col}'] = _infer_value_type(str(row.get('{col}', '')))"


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


# ==============================================================================
# Code Generation - Python Output
# ==============================================================================

def generate_python_code(
    module_name: str,
    data_type: DataType,
    column_transforms: Dict[str, ColumnTransform],
    filter_pred: Optional[FilterPredicate],
    prefix_transforms: List[PrefixTransform],
    window_transforms: List[WindowTransform],
    state_machine_transforms: List[StateMachineTransform],
    neighbor_filter: Optional[NeighborFilter],
) -> str:
    output_columns = [col for col, tr in column_transforms.items() if tr.transform_type != "drop"]
    transform_lines = [_generate_transform_line(col, tr) for col, tr in column_transforms.items() if tr.transform_type != "drop"]
    filter_code = _render_filter_body(filter_pred)
    transform_lines_str = "\n".join(transform_lines)
    output_columns_list = output_columns
    data_type_val = data_type.value

    has_stateful = any([prefix_transforms, window_transforms, state_machine_transforms, neighbor_filter])

    if has_stateful:
        return generate_stateful_python_code(
            module_name, data_type, column_transforms, filter_pred,
            prefix_transforms, window_transforms, state_machine_transforms, neighbor_filter,
            transform_lines_str, output_columns_list, data_type_val
        )
    else:
        return generate_basic_python_code(
            module_name, data_type, column_transforms, filter_pred,
            transform_lines_str, output_columns_list, data_type_val
        )


def generate_basic_python_code(
    module_name: str,
    data_type: DataType,
    column_transforms: Dict[str, ColumnTransform],
    filter_pred: Optional[FilterPredicate],
    transform_lines_str: str,
    output_columns_list: List[str],
    data_type_val: str,
) -> str:
    filter_code = _render_filter_body(filter_pred)

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
{filter_code}


def _apply_transform(row: Dict[str, Any], row_index: int) -> Optional[Dict[str, Any]]:
    output_row = {{}}
{transform_lines_str}
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

    def _get_cache_path(self, input_path: str) -> str:
        key = hashlib.md5(input_path.encode()).hexdigest()
        return os.path.join(self.cache_dir, f"{{key}}_state.json")

    def _load_from_cache(self, input_path: str) -> dict:
        if not self.cache_dir:
            return {{}}
        cache_path = self._get_cache_path(input_path)
        if not os.path.exists(cache_path):
            return {{}}
        try:
            with open(cache_path, 'r') as f:
                return json.load(f)
        except Exception:
            return {{}}

    def _save_to_cache(self, input_path: str, state: dict):
        if not self.cache_dir:
            return
        cache_path = self._get_cache_path(input_path)
        with open(cache_path, 'w') as f:
            json.dump(state, f)

    def _process(self, path: str) -> Iterator[Dict[str, Any]]:
        saved_state = self._load_from_cache(path)
        processed_indices = set(saved_state.get('processed_indices', []))
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

        if self._format == 'csv':
            with open(path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for idx, row in enumerate(reader):
                    if idx in processed_indices:
                        continue
                    buffer_rows.append(row)
                    buffer_indices.append(idx)
                    if len(buffer_rows) >= self.buffer:
                        results, result_indices = process_buffer()
                        for r in results:
                            yield r
                        processed_indices.update(result_indices)
                        buffer_rows, buffer_indices = [], []
                results, result_indices = process_buffer()
                for r in results:
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
                        results, result_indices = process_buffer()
                        for r in results:
                            yield r
                        processed_indices.update(result_indices)
                        buffer_rows, buffer_indices = [], []
                results, result_indices = process_buffer()
                for r in results:
                    yield r

        elif self._format == 'jsonl':
            with open(path, 'r', encoding='utf-8') as f:
                for idx, line in enumerate(f):
                    line = line.strip()
                    if not line:
                        continue
                    if idx in processed_indices:
                        continue
                    try:
                        row = json.loads(line)
                        buffer_rows.append(row)
                        buffer_indices.append(idx)
                        if len(buffer_rows) >= self.buffer:
                            results, result_indices = process_buffer()
                            for r in results:
                                yield r
                            processed_indices.update(result_indices)
                            buffer_rows, buffer_indices = [], []
                    except json.JSONDecodeError:
                        pass
                results, result_indices = process_buffer()
                for r in results:
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
                        results, result_indices = process_buffer()
                        for r in results:
                            yield r
                        processed_indices.update(result_indices)
                        buffer_rows, buffer_indices = [], []
                results, result_indices = process_buffer()
                for r in results:
                    yield r

        self._save_to_cache(path, {{'processed_indices': list(processed_indices)}})
'''


def generate_stateful_python_code(
    module_name: str,
    data_type: DataType,
    column_transforms: Dict[str, ColumnTransform],
    filter_pred: Optional[FilterPredicate],
    prefix_transforms: List[PrefixTransform],
    window_transforms: List[WindowTransform],
    state_machine_transforms: List[StateMachineTransform],
    neighbor_filter: Optional[NeighborFilter],
    transform_lines_str: str,
    output_columns_list: List[str],
    data_type_val: str,
) -> str:
    """Generate Python code with stateful transforms."""
    filter_code = _render_filter_body(filter_pred)

    # Build stateful apply_transform
    stateful_transform_lines = []
    stateful_transform_lines.append("def _apply_stateful_transform(row: Dict[str, Any], state_manager: '_StatefulStateManager', row_index: int) -> Optional[Dict[str, Any]]:")
    stateful_transform_lines.append("    output_row = dict(row)")

    # Clear existing stateful outputs from row
    for pt in prefix_transforms:
        stateful_transform_lines.append(f"    if '{pt.output_column}' in output_row:")
        stateful_transform_lines.append(f"        del output_row['{pt.output_column}']")
    for wt in window_transforms:
        stateful_transform_lines.append(f"    if '{wt.output_column}' in output_row:")
        stateful_transform_lines.append(f"        del output_row['{wt.output_column}']")
    for smt in state_machine_transforms:
        stateful_transform_lines.append(f"    if '{smt.output_column}' in output_row:")
        stateful_transform_lines.append(f"        del output_row['{smt.output_column}']")

    # Apply prefix transforms
    for pt in prefix_transforms:
        if pt.transform_type == "sum":
            stateful_transform_lines.append(f"    output_row['{pt.output_column}'] = state_manager.update_prefix_sum('{pt.input_column}', float(row.get('{pt.input_column}', 0) or 0))")
        elif pt.transform_type == "avg":
            stateful_transform_lines.append(f"    output_row['{pt.output_column}'] = state_manager.update_prefix_avg('{pt.input_column}', float(row.get('{pt.input_column}', 0) or 0))")

    # Apply window transforms
    for wt in window_transforms:
        stateful_transform_lines.append(f"    output_row['{wt.output_column}'] = state_manager.update_window('{wt.input_column}', float(row.get('{wt.input_column}', 0) or 0), {wt.window_size})")

    # Apply state machine transforms
    for smt in state_machine_transforms:
        stateful_transform_lines.append(f"    current_state = state_manager.get_state_machine_initial('{smt.output_column}', {smt.initial_state})")
        stateful_transform_lines.append(f"    new_state = state_manager.update_state_machine('{smt.input_column}', float(row.get('{smt.input_column}', 0) or 0), {smt.threshold}, current_state)")
        stateful_transform_lines.append(f"    output_row['{smt.output_column}'] = new_state")
        stateful_transform_lines.append(f"    state_manager.set_state_machine_state('{smt.output_column}', new_state)")

    # Clear filter-dependent columns for neighbor filter (if any)
    stateful_transform_lines.append("    return output_row")

    stateful_apply_code = "\n".join(stateful_transform_lines)

    # Build state manager class
    state_manager_code = '''
class _StatefulStateManager:
    """Manages state for all stateful transforms."""

    def __init__(self):
        self._prefix_sums = {}
        self._prefix_counts = {}
        self._prefix_avgs = {}
        self._window_buffers = {}
        self._state_machine_states = {}
        self._deferred_rows = []

    def update_prefix_sum(self, col: str, value: float) -> float:
        if col not in self._prefix_sums:
            self._prefix_sums[col] = 0.0
        self._prefix_sums[col] += value
        return self._prefix_sums[col]

    def update_prefix_avg(self, col: str, value: float) -> float:
        if col not in self._prefix_counts:
            self._prefix_counts[col] = 0
            self._prefix_sums[col] = 0.0
        self._prefix_counts[col] += 1
        self._prefix_sums[col] += value
        return self._prefix_sums[col] / self._prefix_counts[col]

    def update_window(self, col: str, value: float, window_size: int) -> float:
        if col not in self._window_buffers:
            self._window_buffers[col] = {}
        if window_size not in self._window_buffers[col]:
            self._window_buffers[col][window_size] = []
        buffer = self._window_buffers[col][window_size]
        buffer.append(value)
        if len(buffer) > window_size:
            buffer.pop(0)
        return sum(buffer) / len(buffer)

    def update_state_machine(self, col: str, value: float, threshold: float, current_state: int) -> int:
        if value >= threshold:
            return current_state + 1
        return current_state

    def get_state_machine_initial(self, col: str, initial: int) -> int:
        if col not in self._state_machine_states:
            self._state_machine_states[col] = initial
        return self._state_machine_states[col]

    def set_state_machine_state(self, col: str, state: int):
        self._state_machine_states[col] = state

    def get_state(self) -> dict:
        return {
            'prefix_sums': self._prefix_sums,
            'prefix_counts': self._prefix_counts,
            'prefix_avgs': self._prefix_avgs,
            'window_buffers': self._window_buffers,
            'state_machine_states': self._state_machine_states,
            'deferred_rows': self._deferred_rows,
        }

    def set_state(self, state: dict):
        self._prefix_sums = state.get('prefix_sums', {})
        self._prefix_counts = state.get('prefix_counts', {})
        self._prefix_avgs = state.get('prefix_avgs', {})
        self._window_buffers = state.get('window_buffers', {})
        self._state_machine_states = state.get('state_machine_states', {})
        self._deferred_rows = state.get('deferred_rows', [])
'''

    # Build the main _apply_transform that uses stateful logic when available
    main_apply_code = '''
def _apply_transform(row: Dict[str, Any], row_index: int) -> Optional[Dict[str, Any]]:
    output_row = {{}}
{transform_lines_str}
    return output_row
'''

    # Generate _apply_transform with stateful processing
    stateful_apply_transform_code = '''
def _apply_transform(row: Dict[str, Any], row_index: int, state_manager: '_StatefulStateManager' = None) -> Optional[Dict[str, Any]]:
    output_row = {{}}
{transform_lines_str}
    return output_row
'''.format(transform_lines_str=transform_lines_str)

    return f'''"""Auto-generated DynamicPreprocessor module with stateful transforms."""

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
{filter_code}


def _apply_transform(row: Dict[str, Any], row_index: int, state_manager: '_StatefulStateManager' = None) -> Optional[Dict[str, Any]]:
    output_row = {{}}
{transform_lines_str}
    return output_row


{state_manager_code}

{stateful_apply_code}


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

    def _get_cache_path(self, input_path: str) -> str:
        key = hashlib.md5(input_path.encode()).hexdigest()
        return os.path.join(self.cache_dir, f"{{key}}_state.json")

    def _load_from_cache(self, input_path: str) -> dict:
        if not self.cache_dir:
            return {{}}
        cache_path = self._get_cache_path(input_path)
        if not os.path.exists(cache_path):
            return {{}}
        try:
            with open(cache_path, 'r') as f:
                return json.load(f)
        except Exception:
            return {{}}

    def _save_to_cache(self, input_path: str, state: dict):
        if not self.cache_dir:
            return
        cache_path = self._get_cache_path(input_path)
        with open(cache_path, 'w') as f:
            json.dump(state, f)

    def _process(self, path: str) -> Iterator[Dict[str, Any]]:
        # Load saved state
        saved_state = self._load_from_cache(path)
        state_manager = _StatefulStateManager()
        state_manager.set_state(saved_state.get('state_manager', {{}}))

        # Get next row index to process
        next_row_idx = saved_state.get('next_row_idx', 0)

        buffer_rows = []
        buffer_indices = []

        def save_state():
            state_dict = {{
                'state_manager': state_manager.get_state(),
                'next_row_idx': next_row_idx + len(buffer_rows),
            }}
            self._save_to_cache(path, state_dict)

        if self._format == 'csv':
            with open(path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for idx, row in enumerate(reader):
                    if idx < next_row_idx:
                        continue
                    buffer_rows.append(row)
                    buffer_indices.append(idx)
                    if len(buffer_rows) >= self.buffer:
                        for row_data, row_idx in zip(buffer_rows, buffer_indices):
                            result = _apply_stateful_transform(row_data, idx, state_manager)
                            if result:
                                yield result
                        next_row_idx = idx + 1
                        buffer_rows, buffer_indices = [], []
                        save_state()
                # Flush remaining
                for row_data, row_idx in zip(buffer_rows, buffer_indices):
                    result = _apply_stateful_transform(row_data, idx, state_manager)
                    if result:
                        yield result

        elif self._format == 'tsv':
            with open(path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f, delimiter='\\t')
                for idx, row in enumerate(reader):
                    if idx < next_row_idx:
                        continue
                    buffer_rows.append(row)
                    buffer_indices.append(idx)
                    if len(buffer_rows) >= self.buffer:
                        for row_data, row_idx in zip(buffer_rows, buffer_indices):
                            result = _apply_stateful_transform(row_data, idx, state_manager)
                            if result:
                                yield result
                        next_row_idx = idx + 1
                        buffer_rows, buffer_indices = [], []
                        save_state()
                for row_data, row_idx in zip(buffer_rows, buffer_indices):
                    result = _apply_stateful_transform(row_data, idx, state_manager)
                    if result:
                        yield result

        elif self._format == 'jsonl':
            with open(path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                for idx, line in enumerate(lines):
                    if idx < next_row_idx:
                        continue
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                        buffer_rows.append(row)
                        buffer_indices.append(idx)
                        if len(buffer_rows) >= self.buffer:
                            for row_data, row_idx in zip(buffer_rows, buffer_indices):
                                result = _apply_stateful_transform(row_data, idx, state_manager)
                                if result:
                                    yield result
                            next_row_idx = idx + 1
                            buffer_rows, buffer_indices = [], []
                            save_state()
                    except json.JSONDecodeError:
                        pass
                for row_data, row_idx in zip(buffer_rows, buffer_indices):
                    result = _apply_stateful_transform(row_data, idx, state_manager)
                    if result:
                        yield result

        elif self._format == 'json':
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if not isinstance(data, list):
                    data = [data]
                for idx, row in enumerate(data):
                    if idx < next_row_idx:
                        continue
                    buffer_rows.append(row)
                    buffer_indices.append(idx)
                    if len(buffer_rows) >= self.buffer:
                        for row_data, row_idx in zip(buffer_rows, buffer_indices):
                            result = _apply_stateful_transform(row_data, idx, state_manager)
                            if result:
                                yield result
                        next_row_idx = idx + 1
                        buffer_rows, buffer_indices = [], []
                        save_state()
                for row_data, row_idx in zip(buffer_rows, buffer_indices):
                    result = _apply_stateful_transform(row_data, idx, state_manager)
                    if result:
                        yield result

        # Final save
        save_state()
'''


# ==============================================================================
# Code Generation - JavaScript Output
# ==============================================================================

def generate_javascript_code(
    module_name: str,
    data_type: DataType,
    column_transforms: Dict[str, ColumnTransform],
    filter_pred: Optional[FilterPredicate],
    prefix_transforms: List[PrefixTransform],
    window_transforms: List[WindowTransform],
    state_machine_transforms: List[StateMachineTransform],
    neighbor_filter: Optional[NeighborFilter],
) -> str:
    output_columns = [col for col, tr in column_transforms.items() if tr.transform_type != "drop"]
    output_columns_list = str(output_columns)
    has_stateful = any([prefix_transforms, window_transforms, state_machine_transforms, neighbor_filter])

    if has_stateful:
        return generate_stateful_javascript_code(
            module_name, data_type, column_transforms, filter_pred,
            prefix_transforms, window_transforms, state_machine_transforms, neighbor_filter,
            output_columns_list
        )
    else:
        return generate_basic_javascript_code(
            module_name, data_type, column_transforms, filter_pred, output_columns_list
        )


def generate_basic_javascript_code(
    module_name: str,
    data_type: DataType,
    column_transforms: Dict[str, ColumnTransform],
    filter_pred: Optional[FilterPredicate],
    output_columns_list: List[str],
) -> str:
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
    return true;
}}

function applyTransform(row, rowIndex) {{
    const outputRow = {{}};
    for (const key in row) {{
        outputRow[key] = inferValueType(row[key]);
    }}
    return outputRow;
}}

class DynamicPreprocessor {{
    constructor(options = {{}}) {{
        this.buffer = options.buffer || 1024;
        this.cacheDir = options.cache_dir || null;
        this.format = '{data_type.value}';
        this.outputColumns = {output_columns_list};
        if (this.cacheDir && !fs.existsSync(this.cacheDir)) {{
            fs.mkdirSync(this.cacheDir, {{ recursive: true }});
        }}
    }}

    process(path) {{
        return this._process(path);
    }}

    _getCachePath(filePath) {{
        const key = crypto.createHash('md5').update(filePath).digest('hex');
        return path.join(this.cacheDir, `${{key}}_state.json`);
    }}

    _loadFromCache(filePath) {{
        if (!this.cacheDir) return {{}};
        const cachePath = this._getCachePath(filePath);
        if (!fs.existsSync(cachePath)) return {{}};
        try {{
            const content = fs.readFileSync(cachePath, 'utf-8');
            return JSON.parse(content);
        }} catch {{
            return {{}};
        }}
    }}

    _saveToCache(filePath, state) {{
        if (!this.cacheDir) return;
        const cachePath = this._getCachePath(filePath);
        fs.writeFileSync(cachePath, JSON.stringify(state));
    }}

    *_process(filePath) {{
        const savedState = this._loadFromCache(filePath);
        const processedIndices = new Set(savedState.processed_indices || []);
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

        const fmt = this.format;

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
                if (bufferRows.length >= this.buffer) {{
                    const {{ results, resultIndices }} = processBuffer();
                    for (const r of results) yield r;
                    for (const idx of resultIndices) processedIndices.add(idx);
                    bufferRows = [];
                    bufferIndices = [];
                }}
            }}
            const {{ results, resultIndices }} = processBuffer();
            for (const r of results) yield r;
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
                if (bufferRows.length >= this.buffer) {{
                    const {{ results, resultIndices }} = processBuffer();
                    for (const r of results) yield r;
                    for (const idx of resultIndices) processedIndices.add(idx);
                    bufferRows = [];
                    bufferIndices = [];
                }}
            }}
            const {{ results, resultIndices }} = processBuffer();
            for (const r of results) yield r;
        }}
        else if (fmt === 'jsonl') {{
            const content = fs.readFileSync(filePath, 'utf-8');
            const lines = content.trim().split('\\n');
            for (let lineNum = 0; lineNum < lines.length; lineNum++) {{
                const idx = lineNum;
                if (processedIndices.has(idx)) continue;
                const line = lines[lineNum].trim();
                if (!line) continue;
                let row;
                try {{ row = JSON.parse(line); }} catch {{ continue; }}
                bufferRows.push(row);
                bufferIndices.push(idx);
                if (bufferRows.length >= this.buffer) {{
                    const {{ results, resultIndices }} = processBuffer();
                    for (const r of results) yield r;
                    for (const idx of resultIndices) processedIndices.add(idx);
                    bufferRows = [];
                    bufferIndices = [];
                }}
            }}
            const {{ results, resultIndices }} = processBuffer();
            for (const r of results) yield r;
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
                if (bufferRows.length >= this.buffer) {{
                    const {{ results, resultIndices }} = processBuffer();
                    for (const r of results) yield r;
                    for (const idx of resultIndices) processedIndices.add(idx);
                    bufferRows = [];
                    bufferIndices = [];
                }}
            }}
            const {{ results, resultIndices }} = processBuffer();
            for (const r of results) yield r;
        }}

        this._saveToCache(filePath, {{ processed_indices: Array.from(processedIndices) }});
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


def generate_stateful_javascript_code(
    module_name: str,
    data_type: DataType,
    column_transforms: Dict[str, ColumnTransform],
    filter_pred: Optional[FilterPredicate],
    prefix_transforms: List[PrefixTransform],
    window_transforms: List[WindowTransform],
    state_machine_transforms: List[StateMachineTransform],
    neighbor_filter: Optional[NeighborFilter],
    output_columns_list: List[str],
) -> str:
    """Generate JavaScript code with stateful transforms."""
    # Build state manager class
    state_manager_code = '''class StateManager {
    constructor() {
        this.prefixSums = {};
        this.prefixCounts = {};
        this.prefixAvgs = {};
        this.windowBuffers = {};
        this.stateMachineStates = {};
        this.deferredRows = [];
    }

    updatePrefixSum(col, value) {
        if (!(col in this.prefixSums)) this.prefixSums[col] = 0;
        this.prefixSums[col] += value;
        return this.prefixSums[col];
    }

    updatePrefixAvg(col, value) {
        if (!(col in this.prefixCounts)) {
            this.prefixCounts[col] = 0;
            this.prefixSums[col] = 0;
        }
        this.prefixCounts[col]++;
        this.prefixSums[col] += value;
        return this.prefixSums[col] / this.prefixCounts[col];
    }

    updateWindow(col, value, windowSize) {
        if (!(col in this.windowBuffers)) this.windowBuffers[col] = {};
        if (!(windowSize in this.windowBuffers[col])) this.windowBuffers[col][windowSize] = [];
        const buffer = this.windowBuffers[col][windowSize];
        buffer.push(value);
        if (buffer.length > windowSize) buffer.shift();
        return buffer.reduce((a, b) => a + b, 0) / buffer.length;
    }

    updateStateMachine(col, value, threshold, currentState) {
        if (value >= threshold) return currentState + 1;
        return currentState;
    }

    getStateMachineInitial(col, initial) {
        if (!(col in this.stateMachineStates)) this.stateMachineStates[col] = initial;
        return this.stateMachineStates[col];
    }

    setStateMachineState(col, state) {
        this.stateMachineStates[col] = state;
    }

    getState() {
        return {
            prefixSums: this.prefixSums,
            prefixCounts: this.prefixCounts,
            prefixAvgs: this.prefixAvgs,
            windowBuffers: this.windowBuffers,
            stateMachineStates: this.stateMachineStates,
            deferredRows: this.deferredRows,
        };
    }

    setState(state) {
        this.prefixSums = state.prefixSums || {};
        this.prefixCounts = state.prefixCounts || {};
        this.prefixAvgs = state.prefixAvgs || {};
        this.windowBuffers = state.windowBuffers || {};
        this.stateMachineStates = state.stateMachineStates || {};
        this.deferredRows = state.deferredRows || [];
    }
}
'''

    # Build applyTransform with stateful processing
    apply_transform_lines = [
        'function applyTransform(row, rowIndex, stateManager) {'
        '    const outputRow = {};'
    ]

    # Apply prefix transforms
    for pt in prefix_transforms:
        if pt.transform_type == "sum":
            apply_transform_lines.append(f"    outputRow['{pt.output_column}'] = stateManager.updatePrefixSum('{pt.input_column}', Number(row['{pt.input_column}'] || 0));")
        elif pt.transform_type == "avg":
            apply_transform_lines.append(f"    outputRow['{pt.output_column}'] = stateManager.updatePrefixAvg('{pt.input_column}', Number(row['{pt.input_column}'] || 0));")

    # Apply window transforms
    for wt in window_transforms:
        apply_transform_lines.append(f"    outputRow['{wt.output_column}'] = stateManager.updateWindow('{wt.input_column}', Number(row['{wt.input_column}'] || 0), {wt.window_size});")

    # Apply state machine transforms
    for smt in state_machine_transforms:
        apply_transform_lines.append(f"    const currentState_{smt.output_column} = stateManager.getStateMachineInitial('{smt.output_column}', {smt.initial_state});")
        apply_transform_lines.append(f"    const newState_{smt.output_column} = stateManager.updateStateMachine('{smt.input_column}', Number(row['{smt.input_column}'] || 0), {smt.threshold}, currentState_{smt.output_column});")
        apply_transform_lines.append(f"    outputRow['{smt.output_column}'] = newState_{smt.output_column};")
        apply_transform_lines.append(f"    stateManager.setStateMachineState('{smt.output_column}', newState_{smt.output_column});")

    apply_transform_lines.append('    return outputRow;')
    apply_transform_lines.append('}')
    apply_transform_code = '\n'.join(apply_transform_lines)

    return f'''// Auto-generated DynamicPreprocessor module with stateful transforms

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
    return true;
}}

{state_manager_code}

{apply_transform_code}

class DynamicPreprocessor {{
    constructor(options = {{}}) {{
        this.buffer = options.buffer || 1024;
        this.cacheDir = options.cache_dir || null;
        this.format = '{data_type.value}';
        this.outputColumns = {output_columns_list};
        if (this.cacheDir && !fs.existsSync(this.cacheDir)) {{
            fs.mkdirSync(this.cacheDir, {{ recursive: true }});
        }}
    }}

    process(path) {{
        return this._process(path);
    }}

    _getCachePath(filePath) {{
        const key = crypto.createHash('md5').update(filePath).digest('hex');
        return path.join(this.cacheDir, `${{key}}_state.json`);
    }}

    _loadFromCache(filePath) {{
        if (!this.cacheDir) return {{}};
        const cachePath = this._getCachePath(filePath);
        if (!fs.existsSync(cachePath)) return {{}};
        try {{
            const content = fs.readFileSync(cachePath, 'utf-8');
            return JSON.parse(content);
        }} catch {{
            return {{}};
        }}
    }}

    _saveToCache(filePath, state) {{
        if (!this.cacheDir) return;
        const cachePath = this._getCachePath(filePath);
        fs.writeFileSync(cachePath, JSON.stringify(state));
    }}

    *_process(filePath) {{
        const savedState = this._loadFromCache(filePath);
        const stateManager = new StateManager();
        stateManager.setState(savedState.stateManager || {{}});

        let nextRowIdx = savedState.next_row_idx || 0;

        const fmt = this.format;

        if (fmt === 'csv') {{
            const content = fs.readFileSync(filePath, 'utf-8');
            const lines = content.trim().split('\\n');
            if (lines.length === 0) return;
            const headers = lines[0].split(',');
            for (let lineNum = 1; lineNum < lines.length; lineNum++) {{
                const idx = lineNum - 1;
                if (idx < nextRowIdx) continue;
                const values = lines[lineNum].split(',');
                const row = {{}};
                for (let i = 0; i < headers.length; i++) {{
                    row[headers[i].trim()] = values[i] !== undefined ? values[i].trim() : '';
                }}
                const result = applyTransform(row, idx, stateManager);
                if (result) yield result;
            }}
        }}
        else if (fmt === 'tsv') {{
            const content = fs.readFileSync(filePath, 'utf-8');
            const lines = content.trim().split('\\n');
            if (lines.length === 0) return;
            const headers = lines[0].split('\\t');
            for (let lineNum = 1; lineNum < lines.length; lineNum++) {{
                const idx = lineNum - 1;
                if (idx < nextRowIdx) continue;
                const values = lines[lineNum].split('\\t');
                const row = {{}};
                for (let i = 0; i < headers.length; i++) {{
                    row[headers[i].trim()] = values[i] !== undefined ? values[i].trim() : '';
                }}
                const result = applyTransform(row, idx, stateManager);
                if (result) yield result;
            }}
        }}
        else if (fmt === 'jsonl') {{
            const content = fs.readFileSync(filePath, 'utf-8');
            const lines = content.trim().split('\\n');
            for (let lineNum = 0; lineNum < lines.length; lineNum++) {{
                const idx = lineNum;
                if (idx < nextRowIdx) continue;
                const line = lines[lineNum].trim();
                if (!line) continue;
                let row;
                try {{ row = JSON.parse(line); }} catch {{ continue; }}
                const result = applyTransform(row, idx, stateManager);
                if (result) yield result;
            }}
        }}
        else if (fmt === 'json') {{
            const content = fs.readFileSync(filePath, 'utf-8');
            let data;
            try {{ data = JSON.parse(content); }} catch {{ return; }}
            if (!Array.isArray(data)) data = [data];
            for (let idx = 0; idx < data.length; idx++) {{
                if (idx < nextRowIdx) continue;
                const row = data[idx];
                const result = applyTransform(row, idx, stateManager);
                if (result) yield result;
            }}
        }}

        this._saveToCache(filePath, {{
            stateManager: stateManager.getState(),
            next_row_idx: nextRowIdx + (fmt === 'csv' ? lines.length - 1 :
                                        fmt === 'tsv' ? lines.length - 1 :
                                        fmt === 'jsonl' ? lines.length :
                                        fmt === 'json' ? (JSON.parse(fs.readFileSync(filePath)).length || 1) : 0)
        }});
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

    args = parser.parse_args()
    target_lang = 'javascript' if args.javascript else 'python'

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
    column_transforms, filter_pred, prefix_transforms, window_transforms, state_machine_transforms, neighbor_filter = \
        infer_transforms(input_data, output_data)

    print(f"Found {len(column_transforms)} column transformations")
    if filter_pred:
        print(f"Found filter predicate: {filter_pred.condition}")
    else:
        print("No filtering applied")
    print(f"Found {len(prefix_transforms)} prefix transforms")
    print(f"Found {len(window_transforms)} window transforms")
    print(f"Found {len(state_machine_transforms)} state machine transforms")
    if neighbor_filter:
        print(f"Found neighbor filter: {neighbor_filter.pattern}")

    data_type = detect_data_format(str(input_file))
    print(f"Generating {target_lang} module...")

    if target_lang == 'python':
        code = generate_python_code(
            args.module_name, data_type, column_transforms, filter_pred,
            prefix_transforms, window_transforms, state_machine_transforms, neighbor_filter
        )
        output_path = Path(args.output) / args.module_name
        output_path.mkdir(parents=True, exist_ok=True)

        init_content = f'''"""Auto-generated package: {args.module_name}"""

from .dynamic_preprocessor import DynamicPreprocessor

__all__ = ['DynamicPreprocessor']
'''
        (output_path / '__init__.py').write_text(init_content, encoding='utf-8')
        (output_path / 'dynamic_preprocessor.py').write_text(code, encoding='utf-8')
        print(f"Generated Python package at: {output_path}")
    else:
        code = generate_javascript_code(
            args.module_name, data_type, column_transforms, filter_pred,
            prefix_transforms, window_transforms, state_machine_transforms, neighbor_filter
        )
        output_path = Path(args.output)
        output_path.mkdir(parents=True, exist_ok=True)
        module_file = output_path / f"{args.module_name}.js"
        module_file.write_text(code, encoding='utf-8')
        print(f"Generated JavaScript module at: {module_file}")

    print("Done!")


if __name__ == '__main__':
    import sys
    main()
