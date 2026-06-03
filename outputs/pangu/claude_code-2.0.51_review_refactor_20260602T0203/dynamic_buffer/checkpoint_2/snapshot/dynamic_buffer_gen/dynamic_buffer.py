#!/usr/bin/env python3
"""
Dynamic Buffer - A code generator that infers transformations from sample data
and generates a DynamicPreprocessor module in Python or JavaScript.
"""

import argparse
import json
import os
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ============================================================================
# Data Classes for Transformation Inference
# ============================================================================

@dataclass
class ColumnTransform:
    """Represents how an input column maps to an output column."""
    input_column: str
    output_column: str
    transform_type: str  # 'identity', 'drop', 'rename', 'copy', 'constant', 'linear', 'string_lower', 'string_upper', 'string_trim', 'string_split', 'string_prefix', 'string_suffix', 'string_combine'
    transform_params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class StatefulTransform:
    """Base class for stateful transforms that require history/context."""
    output_column: str
    input_column: str
    transform_subtype: str  # 'prefix_sum', 'prefix_avg', 'prefix_count', 'sliding_window', 'state_machine', 'neighbor_filter'


@dataclass
class PrefixTransform(StatefulTransform):
    """Cumulative/prefix transform over previous rows."""
    operation: str  # 'sum', 'avg', 'count'
    coefficient: float = 1.0
    constant: float = 0.0
    window_size: Optional[int] = None  # For bounded prefix


@dataclass
class SlidingWindowTransform(StatefulTransform):
    """Fixed-size sliding window transform."""
    window_size: int
    operation: str  # 'sum', 'avg', 'count', 'min', 'max'


@dataclass
class StateMachineTransform(StatefulTransform):
    """State-machine style sequence labeling."""
    initial_state: Any
    states: List[Any]
    transition_rules: List[Dict[str, Any]]  # List of condition->state rules


@dataclass
class NeighborFilterTransform:
    """Neighbor-based filtering based on adjacent rows."""
    filter_type: str  # 'next_row_condition', 'prev_row_condition', 'duplicate_run_first', 'duplicate_run_last'
    column: str
    operator: str
    value: Any


@dataclass
class FilterCondition:
    """Represents a filter condition for row retention."""
    column: str
    operator: str  # 'eq', 'ne', 'lt', 'le', 'gt', 'ge'
    value: Any
    combinator: str = 'and'  # 'and', 'or', 'none'


@dataclass
class InferredTransformation:
    """Complete inferred transformation from input to output."""
    column_transforms: List[ColumnTransform]
    filter_conditions: List[FilterCondition]
    stateful_transforms: List[StatefulTransform]
    neighbor_filters: List[NeighborFilterTransform]
    input_columns: List[str]
    output_columns: List[str]
    file_format: str


# ============================================================================
# File Reader/Writer
# ============================================================================

def detect_format(filepath: str) -> str:
    """Detect file format from extension."""
    ext = Path(filepath).suffix.lower().lstrip('.')
    if ext not in ('csv', 'tsv', 'jsonl', 'json'):
        raise ValueError(f"Unsupported file format: {ext}")
    return ext


def read_sample_data(filepath: str, fmt: str) -> List[Dict[str, Any]]:
    """Read sample data file and return list of row dicts."""
    rows = []

    if fmt == 'csv':
        with open(filepath, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Convert values to appropriate types
                converted = {}
                for k, v in row.items():
                    converted[k] = _convert_value(v)
                rows.append(converted)

    elif fmt == 'tsv':
        with open(filepath, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f, delimiter='\t')
            for row in reader:
                converted = {}
                for k, v in row.items():
                    converted[k] = _convert_value(v)
                rows.append(converted)

    elif fmt == 'jsonl':
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))

    elif fmt == 'json':
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
            if isinstance(data, list):
                rows = data
            else:
                raise ValueError("JSON file must contain an array")

    return rows


def _convert_value(value: str) -> Any:
    """Convert string value to appropriate type."""
    if not value:
        return None

    # Try boolean
    lower = value.lower()
    if lower in ('true', 'false'):
        return lower == 'true'

    # Try integer
    try:
        return int(value)
    except ValueError:
        pass

    # Try float
    try:
        return float(value)
    except ValueError:
        pass

    # Return as string
    return value


def _format_value_for_code(value: Any, lang: str) -> str:
    """Format a value for use in generated code."""
    if lang == 'python':
        if value is None:
            return 'None'
        elif isinstance(value, bool):
            return 'True' if value else 'False'
        elif isinstance(value, (int, float)):
            return str(value)
        else:
            return repr(str(value))
    else:  # javascript
        if value is None:
            return 'null'
        elif isinstance(value, bool):
            return 'true' if value else 'false'
        elif isinstance(value, (int, float)):
            return str(value)
        else:
            return json.dumps(str(value))


# ============================================================================
# Transformation Inference
# ============================================================================

def infer_transformation(input_rows: List[Dict], output_rows: List[Dict], fmt: str) -> InferredTransformation:
    """Infer the transformation from input to output samples."""

    # Get column sets
    input_cols = set()
    for row in input_rows:
        input_cols.update(row.keys())

    output_cols = set()
    for row in output_rows:
        output_cols.update(row.keys())

    # Convert to sorted lists for deterministic order
    input_cols_list = sorted(input_cols)
    output_cols_list = sorted(output_cols)

    # Determine which rows were kept (filter inference)
    kept_indices = _find_kept_indices(input_rows, output_rows)

    # Infer filter conditions (both simple and neighbor-based)
    filter_conditions, neighbor_filters = _infer_filters_and_neighbors(
        input_rows, output_rows, kept_indices
    )

    # Infer column transformations
    column_transforms, stateful_transforms = _infer_column_and_stateful_transforms(
        input_rows, output_rows, kept_indices, input_cols_list, output_cols_list
    )

    return InferredTransformation(
        column_transforms=column_transforms,
        filter_conditions=filter_conditions,
        stateful_transforms=stateful_transforms,
        neighbor_filters=neighbor_filters,
        input_columns=input_cols_list,
        output_columns=output_cols_list,
        file_format=fmt
    )


def _find_kept_indices(input_rows: List[Dict], output_rows: List[Dict]) -> List[int]:
    """Find which input rows correspond to output rows."""
    kept = []
    output_idx = 0

    for i, input_row in enumerate(input_rows):
        if output_idx < len(output_rows):
            output_row = output_rows[output_idx]
            # Check if rows match (comparing all values)
            if _rows_match(input_row, output_row):
                kept.append(i)
                output_idx += 1

    # Handle case where filtering might have occurred
    if len(kept) != len(output_rows):
        # Use a more robust matching based on key columns
        kept = _find_kept_indices_robust(input_rows, output_rows)

    return kept


def _rows_match(row1: Dict, row2: Dict) -> bool:
    """Check if two rows are equivalent (considering row identity)."""
    if set(row1.keys()) != set(row2.keys()):
        return False
    return all(row1[k] == row2[k] for k in row1)


def _find_kept_indices_robust(input_rows: List[Dict], output_rows: List[Dict]) -> List[int]:
    """More robust method to find kept indices when simple matching fails."""
    kept = []
    used = set()

    for output_row in output_rows:
        for i, input_row in enumerate(input_rows):
            if i in used:
                continue
            # Check if input row matches output row in all output columns
            match = True
            for col in output_row:
                if col in input_row and input_row[col] == output_row[col]:
                    continue
                else:
                    match = False
                    break
            if match:
                kept.append(i)
                used.add(i)
                break

    return kept


def _infer_filters_and_neighbors(input_rows: List[Dict], output_rows: List[Dict], kept_indices: List[int]) -> Tuple[List[FilterCondition], List[NeighborFilterTransform]]:
    """Infer filter conditions and neighbor-based filters."""
    filters = []
    neighbor_filters = []

    if len(input_rows) == len(output_rows):
        return filters, neighbor_filters  # No filtering

    kept_set = set(kept_indices)
    dropped_set = set(range(len(input_rows))) - kept_set

    # Try to infer neighbor-based filtering
    # Pattern 1: Drop row if next row has certain value
    neighbor_filter = _infer_next_row_filter(input_rows, output_rows, kept_indices)
    if neighbor_filter:
        neighbor_filters.append(neighbor_filter)
        # Update kept_indices based on this filter
        kept_indices = [i for i in kept_indices if not _should_drop_by_next(input_rows, i, neighbor_filter)]

    # Also check for duplicate run filtering (keep first/last)
    dup_filter = _infer_duplicate_run_filter(input_rows, output_rows, kept_indices)
    if dup_filter:
        neighbor_filters.append(dup_filter)
        # Update kept_indices accordingly

    # Try simple filters for remaining drops
    all_cols = set()
    for row in input_rows + output_rows:
        all_cols.update(row.keys())

    for col in all_cols:
        kept_values = set()
        dropped_values = set()

        for i, row in enumerate(input_rows):
            if col in row:
                if i in kept_set:
                    kept_values.add(row[col])
                else:
                    dropped_values.add(row[col])

        if dropped_values and len(dropped_values) < len(kept_values):
            if len(dropped_values) == 1:
                val = next(iter(dropped_values))
                filters.append(FilterCondition(
                    column=col,
                    operator='ne',
                    value=val
                ))

    return filters, neighbor_filters


def _infer_next_row_filter(input_rows: List[Dict], output_rows: List[Dict], kept_indices: List[int]) -> Optional[NeighborFilterTransform]:
    """Infer if filtering depends on next row's values."""
    kept_set = set(kept_indices)

    # Check if any dropped row has a next row with certain property
    for i in range(len(input_rows) - 1):
        if i not in kept_set:
            next_row = input_rows[i + 1]
            # Check if next row has values that appear consistently
            for col, val in next_row.items():
                # Count how many dropped rows have next row with this value
                count = 0
                for j in range(len(input_rows) - 1):
                    if j not in kept_set and input_rows[j + 1].get(col) == val:
                        count += 1
                # If this pattern is common, it's likely a next-row filter
                if count >= 2:  # At least 2 dropped rows share this pattern
                    return NeighborFilterTransform(
                        filter_type='next_row_condition',
                        column=col,
                        operator='eq',
                        value=val
                    )
    return None


def _infer_duplicate_run_filter(input_rows: List[Dict], output_rows: List[Dict], kept_indices: List[int]) -> Optional[NeighborFilterTransform]:
    """Infer duplicate run filtering (keep first or last of duplicate runs)."""
    kept_set = set(kept_indices)

    for col in input_rows[0].keys() if input_rows else []:
        # Find runs of duplicate values
        runs = []
        current_run = []
        for i, row in enumerate(input_rows):
            if not current_run or row.get(col) == input_rows[current_run[0]].get(col):
                current_run.append(i)
            else:
                if len(current_run) > 1:
                    runs.append(current_run)
                current_run = [i]
        if len(current_run) > 1:
            runs.append(current_run)

        # Check if we're keeping first or last of each run
        if runs:
            keeping_first = all(run[0] in kept_set for run in runs)
            keeping_last = all(run[-1] in kept_set for run in runs)

            if keeping_first:
                return NeighborFilterTransform(
                    filter_type='duplicate_run_first',
                    column=col,
                    operator='eq',
                    value=None  # Not used for this type
                )
            elif keeping_last:
                return NeighborFilterTransform(
                    filter_type='duplicate_run_last',
                    column=col,
                    operator='eq',
                    value=None
                )
    return None


def _should_drop_by_next(input_rows: List[Dict], idx: int, neighbor_filter: NeighborFilterTransform) -> bool:
    """Check if a row should be dropped based on next row."""
    if idx >= len(input_rows) - 1:
        return False
    next_val = input_rows[idx + 1].get(neighbor_filter.column)
    return next_val == neighbor_filter.value


def _infer_prefix_transform(input_rows: List[Dict], output_rows: List[Dict], kept_indices: List[int], out_col: str, input_cols: List[str]) -> Optional[PrefixTransform]:
    """Infer a prefix/cumulative transform (e.g., running sum)."""
    for in_col in input_cols:
        # Check if input column is numeric
        numeric_values = []
        for idx in kept_indices:
            if idx < len(input_rows):
                val = input_rows[idx].get(in_col)
                if isinstance(val, (int, float)):
                    numeric_values.append((idx, val))

        if len(numeric_values) < 2:
            continue

        # Check for prefix sum pattern
        prefix_sum = 0
        matches_sum = True
        for i, (idx, val) in enumerate(numeric_values):
            prefix_sum += val
            out_idx = kept_indices.index(idx)
            if out_idx < len(output_rows):
                out_val = output_rows[out_idx].get(out_col)
                if not isinstance(out_val, (int, float)) or abs(out_val - prefix_sum) > 0.001:
                    matches_sum = False
                    break

        if matches_sum:
            return PrefixTransform(
                output_column=out_col,
                input_column=in_col,
                transform_subtype='prefix_sum',
                operation='sum'
            )

        # Check for prefix average pattern
        prefix_avg_vals = []
        matches_avg = True
        running_sum = 0
        for i, (idx, val) in enumerate(numeric_values):
            running_sum += val
            avg_val = running_sum / (i + 1)
            prefix_avg_vals.append(avg_val)
            out_idx = kept_indices.index(idx)
            if out_idx < len(output_rows):
                out_val = output_rows[out_idx].get(out_col)
                if not isinstance(out_val, (int, float)) or abs(out_val - avg_val) > 0.001:
                    matches_avg = False
                    break

        if matches_avg:
            return PrefixTransform(
                output_column=out_col,
                input_column=in_col,
                transform_subtype='prefix_avg',
                operation='avg'
            )

        # Check for prefix count pattern
        matches_count = True
        for i, (idx, val) in enumerate(numeric_values):
            out_idx = kept_indices.index(idx)
            if out_idx < len(output_rows):
                out_val = output_rows[out_idx].get(out_col)
                # Should be (i + 1) for count
                if not isinstance(out_val, (int, float)) or abs(out_val - (i + 1)) > 0.001:
                    matches_count = False
                    break

        if matches_count and len(kept_indices) >= 2:
            return PrefixTransform(
                output_column=out_col,
                input_column=in_col,
                transform_subtype='prefix_count',
                operation='count'
            )

    return None


def _infer_sliding_window_transform(input_rows: List[Dict], output_rows: List[Dict], kept_indices: List[int], out_col: str, input_cols: List[str]) -> Optional[SlidingWindowTransform]:
    """Infer a fixed-size sliding window transform."""
    for in_col in input_cols:
        for window_size in range(1, min(65, len(kept_indices) + 1)):
            # Check for sliding window sum
            matches_sum = True
            for i, idx in enumerate(kept_indices):
                # Sum of last W values (including current)
                start = max(0, i - window_size + 1)
                window_values = []
                for j in range(start, i + 1):
                    if j < len(input_rows):
                        val = input_rows[kept_indices[j]].get(in_col)
                        if isinstance(val, (int, float)):
                            window_values.append(val)

                expected_sum = sum(window_values)
                out_idx = kept_indices.index(idx)
                if out_idx < len(output_rows):
                    out_val = output_rows[out_idx].get(out_col)
                    if not isinstance(out_val, (int, float)) or abs(out_val - expected_sum) > 0.01:
                        matches_sum = False
                        break

            if matches_sum:
                return SlidingWindowTransform(
                    output_column=out_col,
                    input_column=in_col,
                    transform_subtype='sliding_window',
                    window_size=window_size,
                    operation='sum'
                )

            # Check for sliding window average
            matches_avg = True
            for i, idx in enumerate(kept_indices):
                start = max(0, i - window_size + 1)
                window_values = []
                for j in range(start, i + 1):
                    if j < len(input_rows):
                        val = input_rows[kept_indices[j]].get(in_col)
                        if isinstance(val, (int, float)):
                            window_values.append(val)

                expected_avg = sum(window_values) / len(window_values) if window_values else 0
                out_idx = kept_indices.index(idx)
                if out_idx < len(output_rows):
                    out_val = output_rows[out_idx].get(out_col)
                    if not isinstance(out_val, (int, float)) or abs(out_val - expected_avg) > 0.01:
                        matches_avg = False
                        break

            if matches_avg:
                return SlidingWindowTransform(
                    output_column=out_col,
                    input_column=in_col,
                    transform_subtype='sliding_window',
                    window_size=window_size,
                    operation='avg'
                )

    return None


def _infer_state_machine_transform(input_rows: List[Dict], output_rows: List[Dict], kept_indices: List[int], out_col: str, input_cols: List[str]) -> Optional[StateMachineTransform]:
    """Infer a state-machine style labeling (e.g., segment IDs)."""
    # Check if output column contains sequential integer IDs
    if not kept_indices:
        return None

    # Get output values for this column
    out_values = []
    for idx in kept_indices:
        out_idx = kept_indices.index(idx)
        if out_idx < len(output_rows):
            out_values.append((idx, output_rows[out_idx].get(out_col)))

    # Check if values are integers
    if not all(isinstance(v, (int, float)) for _, v in out_values):
        return None

    # Check for pattern: values increment based on some condition in input
    # Simple case: check if we can find an input column that triggers state changes
    for in_col in input_cols:
        numeric_in = []
        for idx, out_val in out_values:
            in_val = input_rows[idx].get(in_col)
            if isinstance(in_val, (int, float)):
                numeric_in.append((idx, in_val, out_val))

        if len(numeric_in) < 2:
            continue

        # Analyze state transitions
        # Look for threshold-based state changes
        thresholds = set()
        for i in range(1, len(numeric_in)):
            prev_in = numeric_in[i-1][1]
            curr_in = numeric_in[i][1]
            prev_state = numeric_in[i-1][2]
            curr_state = numeric_in[i][2]

            if curr_state != prev_state:
                # Check if crossing a threshold triggers the change
                # Either curr_in > threshold or curr_in < threshold
                if prev_in < curr_in:  # Increasing
                    thresholds.add((curr_in, curr_state))
                else:  # Decreasing
                    thresholds.add((curr_in, curr_state))

        # Verify this is a legit state machine
        # States should be small integers
        unique_states = sorted(set(v for _, _, v in numeric_in))
        if not unique_states:
            continue

        # Check if transitions follow a pattern based on a threshold
        # Try threshold at ~1.0 for level-based segmentation (from example)
        if len(thresholds) > 0:
            threshold_val = sorted(thresholds)[0][0]

            # Test if threshold prediction works for all transitions
            matches = True
            expected_states = []
            current_state = unique_states[0]

            for idx, in_val, _ in numeric_in:
                # Apply transition rule based on threshold
                if in_val >= threshold_val and current_state != 2:
                    current_state = 2  # State after threshold
                elif in_val < threshold_val and current_state != 1:
                    current_state = 1  # State before threshold
                expected_states.append((idx, current_state))

            # Compare with actual
            for (idx, _), (_, expected) in zip(numeric_in, expected_states):
                out_idx = kept_indices.index(idx)
                actual = output_rows[out_idx].get(out_col)
                if actual != expected:
                    matches = False
                    break

            if matches and len(unique_states) <= 16:
                return StateMachineTransform(
                    output_column=out_col,
                    input_column=in_col,
                    transform_subtype='state_machine',
                    initial_state=unique_states[0],
                    states=unique_states,
                    transition_rules=[{
                        'condition_column': in_col,
                        'threshold': threshold_val,
                        'new_state': 2
                    }]
                )

    return None


def _infer_column_and_stateful_transforms(
    input_rows: List[Dict],
    output_rows: List[Dict],
    kept_indices: List[int],
    input_cols: List[str],
    output_cols: List[str]
) -> Tuple[List[ColumnTransform], List[StatefulTransform]]:
    """Infer how each output column is derived from input columns, including stateful transforms."""
    transforms = []
    stateful_transforms = []

    # Track which columns are used in output (for simple transforms)
    used_input_cols = set()
    stateful_output_cols = set()

    for out_col in output_cols:
        best_transform = None
        best_score = 0

        # First try simple transforms
        # Try identity transformation (same column name)
        if out_col in input_cols:
            if _verify_identity(input_rows, output_rows, kept_indices, out_col, out_col):
                best_transform = ColumnTransform(
                    input_column=out_col,
                    output_column=out_col,
                    transform_type='identity',
                    transform_params={}
                )
                best_score = 100
                used_input_cols.add(out_col)

        # Try constant value
        if best_score == 0:
            const_val = _infer_constant(input_rows, output_rows, kept_indices, out_col)
            if const_val is not None:
                best_transform = ColumnTransform(
                    input_column='',
                    output_column=out_col,
                    transform_type='constant',
                    transform_params={'value': const_val}
                )
                best_score = 90

        # Try linear transformation for numeric columns
        if best_score == 0:
            lin_result = _infer_linear_transform(input_rows, output_rows, kept_indices, out_col, input_cols)
            if lin_result:
                in_col, a, b = lin_result
                if a != 1 or b != 0:  # Only if transformation exists
                    best_transform = ColumnTransform(
                        input_column=in_col,
                        output_column=out_col,
                        transform_type='linear',
                        transform_params={'a': a, 'b': b}
                    )
                    best_score = 85
                    used_input_cols.add(in_col)

        # Try string transformations
        if best_score < 85:
            str_result = _infer_string_transform(input_rows, output_rows, kept_indices, out_col, input_cols)
            if str_result:
                in_col, transform_type, params = str_result
                best_transform = ColumnTransform(
                    input_column=in_col,
                    output_column=out_col,
                    transform_type=transform_type,
                    transform_params=params
                )
                best_score = 80
                if in_col:
                    used_input_cols.add(in_col)

        # Try copy from another column
        if best_score < 80:
            for in_col in input_cols:
                if in_col != out_col and _verify_identity(input_rows, output_rows, kept_indices, in_col, out_col):
                    best_transform = ColumnTransform(
                        input_column=in_col,
                        output_column=out_col,
                        transform_type='copy',
                        transform_params={}
                    )
                    best_score = 75
                    used_input_cols.add(in_col)
                    break

        # Try stateful transforms (prefix, sliding window, state machine)
        if best_score == 0:
            stateful = _infer_prefix_transform(input_rows, output_rows, kept_indices, out_col, input_cols)
            if stateful:
                stateful_transforms.append(stateful)
                stateful_output_cols.add(out_col)
                best_score = 70
                continue

        if best_score == 0:
            stateful = _infer_sliding_window_transform(input_rows, output_rows, kept_indices, out_col, input_cols)
            if stateful:
                stateful_transforms.append(stateful)
                stateful_output_cols.add(out_col)
                best_score = 70
                continue

        if best_score == 0:
            stateful = _infer_state_machine_transform(input_rows, output_rows, kept_indices, out_col, input_cols)
            if stateful:
                stateful_transforms.append(stateful)
                stateful_output_cols.add(out_col)
                best_score = 70
                continue

        if best_transform:
            transforms.append(best_transform)

    # Add drop transformations for unused input columns
    for in_col in input_cols:
        if in_col not in used_input_cols and in_col not in output_cols and in_col not in stateful_output_cols:
            transforms.append(ColumnTransform(
                input_column=in_col,
                output_column=in_col,
                transform_type='drop',
                transform_params={}
            ))

    return transforms, stateful_transforms


def _verify_identity(
    input_rows: List[Dict],
    output_rows: List[Dict],
    kept_indices: List[int],
    in_col: str,
    out_col: str
) -> bool:
    """Check if output column equals input column for all kept rows."""
    for idx in kept_indices:
        if idx >= len(input_rows):
            return False
        in_val = input_rows[idx].get(in_col)
        out_val = output_rows[kept_indices.index(idx) if idx in kept_indices else -1].get(out_col) if kept_indices else None
        if kept_indices and idx in kept_indices:
            out_idx = kept_indices.index(idx)
            if out_idx < len(output_rows):
                out_val = output_rows[out_idx].get(out_col)
                if in_val != out_val:
                    return False
    return True


def _infer_constant(
    input_rows: List[Dict],
    output_rows: List[Dict],
    kept_indices: List[int],
    out_col: str
) -> Any:
    """Check if output column is constant across all rows."""
    if not kept_indices:
        return None

    first_val = None
    for idx in kept_indices:
        if idx < len(output_rows):
            val = output_rows[idx].get(out_col) if idx < len(output_rows) else None
            if first_val is None:
                first_val = val
            elif val != first_val:
                return None

    return first_val


def _infer_linear_transform(
    input_rows: List[Dict],
    output_rows: List[Dict],
    kept_indices: List[int],
    out_col: str,
    input_cols: List[str]
) -> Optional[Tuple[str, float, float]]:
    """Infer if output column is a linear transform of an input numeric column."""
    for in_col in input_cols:
        # Check if columns contain numeric data
        numeric_pairs = []
        for idx in kept_indices:
            if idx < len(input_rows) and idx < len(output_rows):
                in_val = input_rows[idx].get(in_col)
                out_val = output_rows[kept_indices.index(idx)].get(out_col)

                if isinstance(in_val, (int, float)) and isinstance(out_val, (int, float)):
                    numeric_pairs.append((float(in_val), float(out_val)))

        if len(numeric_pairs) >= 2:
            # Try to find linear relationship y = a*x + b
            # Use first two points to determine a and b
            x1, y1 = numeric_pairs[0]
            x2, y2 = numeric_pairs[1]

            if x2 != x1:
                a = (y2 - y1) / (x2 - x1)
                b = y1 - a * x1

                # Verify with other points
                for x, y in numeric_pairs[2:]:
                    predicted = a * x + b
                    if abs(predicted - y) > 1e-6:
                        break
                else:
                    return (in_col, a, b)

    return None


def _infer_string_transform(
    input_rows: List[Dict],
    output_rows: List[Dict],
    kept_indices: List[int],
    out_col: str,
    input_cols: List[str]
) -> Optional[Tuple[str, str, Dict]]:
    """Infer string transformations."""
    for in_col in input_cols:
        # Check if it's a lower transformation
        if _verify_string_op(input_rows, output_rows, kept_indices, in_col, out_col, 'lower'):
            return (in_col, 'string_lower', {})

        # Check if it's an upper transformation
        if _verify_string_op(input_rows, output_rows, kept_indices, in_col, out_col, 'upper'):
            return (in_col, 'string_upper', {})

        # Check if it's a trim transformation
        if _verify_string_op(input_rows, output_rows, kept_indices, in_col, out_col, 'trim'):
            return (in_col, 'string_trim', {})

    # Check if it's a combination of columns
    if len(input_cols) >= 2:
        for combo_test in _test_combinations(input_rows, output_rows, kept_indices, out_col, input_cols):
            return combo_test

    return None


def _verify_string_op(
    input_rows: List[Dict],
    output_rows: List[Dict],
    kept_indices: List[int],
    in_col: str,
    out_col: str,
    op: str
) -> bool:
    """Verify if output column is a string transformation of input column."""
    for idx in kept_indices:
        if idx < len(input_rows) and idx < len(output_rows):
            in_val = input_rows[idx].get(in_col)
            out_val = output_rows[kept_indices.index(idx)].get(out_col)

            if not isinstance(in_val, str) or not isinstance(out_val, str):
                return False

            if op == 'lower' and in_val.lower() != out_val:
                return False
            elif op == 'upper' and in_val.upper() != out_val:
                return False
            elif op == 'trim' and in_val.strip() != out_val:
                return False

    # Check all rows match the transformation
    for idx in kept_indices:
        if idx < len(input_rows) and idx < len(output_rows):
            in_val = input_rows[idx].get(in_col)
            out_val = output_rows[kept_indices.index(idx)].get(out_col)

            if in_val is None and out_val is None:
                continue
            if not isinstance(in_val, str) or not isinstance(out_val, str):
                return False

            transformed = None
            if op == 'lower':
                transformed = in_val.lower()
            elif op == 'upper':
                transformed = in_val.upper()
            elif op == 'trim':
                transformed = in_val.strip()

            if transformed != out_val:
                return False

    return True


def _test_combinations(
    input_rows: List[Dict],
    output_rows: List[Dict],
    kept_indices: List[int],
    out_col: str,
    input_cols: List[str]
) -> List[Tuple[str, str, Dict]]:
    """Test if output column is a combination of input columns."""
    results = []

    # Test prefix + column
    for in_col in input_cols:
        for idx in kept_indices:
            if idx < len(input_rows) and idx < len(output_rows):
                in_val = input_rows[idx].get(in_col)
                out_val = output_rows[kept_indices.index(idx)].get(out_col)

                if isinstance(in_val, str) and isinstance(out_val, str):
                    # Check if out_val starts with in_val + something
                    if out_val.startswith(in_val):
                        prefix = in_val
                        suffix = out_val[len(in_val):]
                        # Verify all rows match this pattern
                        match = True
                        for i in kept_indices:
                            if i < len(input_rows):
                                v_in = input_rows[i].get(in_col)
                                v_out = output_rows[kept_indices.index(i)].get(out_col)
                                if isinstance(v_in, str) and isinstance(v_out, str):
                                    if not v_out.startswith(v_in):
                                        match = False
                                        break
                        if match:
                            results.append((in_col, 'string_prefix', {'prefix': prefix}))

    # Test column + suffix
    for in_col in input_cols:
        for idx in kept_indices:
            if idx < len(input_rows) and idx < len(output_rows):
                in_val = input_rows[idx].get(in_col)
                out_val = output_rows[kept_indices.index(idx)].get(out_col)

                if isinstance(in_val, str) and isinstance(out_val, str):
                    if out_val.endswith(in_val):
                        suffix = out_val[:-len(in_val)]
                        match = True
                        for i in kept_indices:
                            if i < len(input_rows):
                                v_in = input_rows[i].get(in_col)
                                v_out = output_rows[kept_indices.index(i)].get(out_col)
                                if isinstance(v_in, str) and isinstance(v_out, str):
                                    if not v_out.endswith(v_in):
                                        match = False
                                        break
                        if match:
                            results.append((in_col, 'string_suffix', {'suffix': suffix}))

    return results


# ============================================================================
# Code Generators
# ============================================================================

def generate_python_module(transform: InferredTransformation, output_dir: str, module_name: str):
    """Generate Python module for the transformation."""
    module_path = Path(output_dir) / module_name
    module_path.mkdir(parents=True, exist_ok=True)

    # Generate __init__.py
    init_content = f'''"""Auto-generated module for DynamicPreprocessor."""

from .dynamic_preprocessor import DynamicPreprocessor

__all__ = ['DynamicPreprocessor']
'''
    (module_path / '__init__.py').write_text(init_content, encoding='utf-8')

    # Generate dynamic_preprocessor.py
    preprocessor_content = _generate_python_preprocessor(transform)
    (module_path / 'dynamic_preprocessor.py').write_text(preprocessor_content, encoding='utf-8')


def _generate_python_preprocessor(transform: InferredTransformation) -> str:
    """Generate the Python DynamicPreprocessor implementation with stateful transforms."""

    filter_code = _generate_python_filter(transform.filter_conditions)
    transform_code, row_transform_func = _generate_python_row_transform(transform)
    reader_setup = _generate_python_reader_setup(transform.file_format)
    stateful_init = _generate_stateful_init_code(transform)
    stateful_methods = _generate_stateful_methods()

    return f'''"""DynamicPreprocessor implementation for transformed data streaming."""

import csv
import json
import os
from pathlib import Path
from typing import Dict, Iterator, Any, Optional, List


class DynamicPreprocessor:
    """
    A streaming preprocessor that applies inferred transformations to data files.

    Args:
        buffer: Maximum number of rows to keep in memory at once.
        cache_dir: Optional directory for caching and resuming processing.
    """

    def __init__(self, buffer: int, cache_dir: Optional[str] = None):
        self.buffer = buffer
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._input_columns = {transform.input_columns}
        self._output_columns = {transform.output_columns}
        self._file_format = '{transform.file_format}'
{stateful_init}
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def __call__(self, path: str) -> Iterator[Dict[str, Any]]:
        """Process a data file and yield transformed rows."""
        return self._process_file(path)

    def _process_file(self, path: str) -> Iterator[Dict[str, Any]]:
        """Process a file with streaming, stateful transforms, and optional caching."""
        path_obj = Path(path)
        cache_file = None
        resume_from = 0

        # Setup cache if enabled
        if self.cache_dir:
            cache_key = path_obj.stem.replace('.', '_')
            cache_file = self.cache_dir / f"{{cache_key}}_cache.json"
            index_file = self.cache_dir / f"{{cache_key}}_index.txt"

            # Check for existing cache
            if cache_file.exists() and index_file.exists():
                with open(index_file, 'r') as f:
                    resume_from = int(f.read().strip())

                # Yield cached rows first
                with open(cache_file, 'r') as f:
                    cached_rows = json.load(f)
                for row in cached_rows:
                    yield row

        # Process file with streaming
        rows_buffer = []
        current_idx = 0

        for raw_row in self._read_file(path):
            if current_idx < resume_from:
                # Skip, but update state for stateful transforms to stay synchronized
                self._update_state(raw_row)
                current_idx += 1
                continue

            # Apply transformation
            transformed = {row_transform_func}

            # Apply filter
            if {filter_code}:
                rows_buffer.append(transformed)

                if len(rows_buffer) >= self.buffer:
                    # Yield buffered rows and update cache
                    for row in rows_buffer:
                        yield row

                    if cache_file:
                        self._update_cache(cache_file, rows_buffer)

                    rows_buffer = []

            current_idx += 1

        # Yield remaining rows
        for row in rows_buffer:
            yield row

        # Update cache with final rows
        if cache_file and rows_buffer:
            self._update_cache(cache_file, rows_buffer)

        # Update index
        if self.cache_dir and cache_file:
            index_file = self.cache_dir / f"{{cache_file.stem.split('_')[0]}}_index.txt"
            with open(index_file, 'w') as f:
                f.write(str(current_idx))

    def _read_file(self, path: str) -> Iterator[Dict[str, Any]]:
        """Read file based on its format."""
        {reader_setup}

    def _update_cache(self, cache_file: Path, rows: List[Dict]):
        """Append rows to cache file."""
        existing = []
        if cache_file.exists():
            with open(cache_file, 'r') as f:
                existing = json.load(f)
        existing.extend(rows)
        with open(cache_file, 'w') as f:
            json.dump(existing, f)

    def _save_state(self, path: Path):
        """Save internal state to disk for resuming."""
        state = {{
            'prefix_sums': self._prefix_sums,
            'prefix_counts': self._prefix_counts,
            'prefix_totals': self._prefix_totals,
            'sliding_windows': self._sliding_windows,
            'state_machines': self._state_machines,
            'row_buffer': self._row_buffer,
        }}
        with open(path, 'w') as f:
            json.dump(state, f)

    def _load_state(self, path: Path) -> bool:
        """Load internal state from disk. Returns True if successful."""
        if not path.exists():
            return False
        try:
            with open(path, 'r') as f:
                state = json.load(f)

            # Restore state
            self._prefix_sums = state.get('prefix_sums', {{}})
            self._prefix_counts = state.get('prefix_counts', {{}})
            self._prefix_totals = state.get('prefix_totals', {{}})
            self._sliding_windows = state.get('sliding_windows', {{}})
            self._state_machines = state.get('state_machines', {{}})
            self._row_buffer = state.get('row_buffer', [])
            return True
        except Exception:
            return False

    def _save_state_for_row(self, path: Path, row_idx: int):
        """Save state at a specific row position."""
        state = {{
            'row_idx': row_idx,
            'prefix_sums': self._prefix_sums,
            'prefix_counts': self._prefix_counts,
            'prefix_totals': self._prefix_totals,
            'sliding_windows': self._sliding_windows,
            'state_machines': self._state_machines,
            'row_buffer': self._row_buffer,
        }}
        # Atomic write
        temp_path = path.with_suffix('.tmp')
        with open(temp_path, 'w') as f:
            json.dump(state, f)
        temp_path.replace(path)

    def _load_state_for_row(self, path: Path) -> Optional[int]:
        """Load state for a specific row position. Returns row_idx or None."""
        if not path.exists():
            return None
        try:
            with open(path, 'r') as f:
                state = json.load(f)

            self._prefix_sums = state.get('prefix_sums', {{}})
            self._prefix_counts = state.get('prefix_counts', {{}})
            self._prefix_totals = state.get('prefix_totals', {{}})
            self._sliding_windows = state.get('sliding_windows', {{}})
            self._state_machines = state.get('state_machines', {{}})
            self._row_buffer = state.get('row_buffer', [])
            return state.get('row_idx')
        except Exception:
            return None

{stateful_methods}


def transform_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    Apply the inferred transformation to a single row.

    {transform_code}
    """
    return result
'''


def _generate_python_filter(conditions: List[FilterCondition]) -> str:
    """Generate Python filter code."""
    if not conditions:
        return 'True'

    op_map = {
        'eq': '==',
        'ne': '!=',
        'lt': '<',
        'le': '<=',
        'gt': '>',
        'ge': '>='
    }

    parts = []
    for cond in conditions:
        value_str = _format_value_for_code(cond.value, 'python')
        parts.append(f"row.get('{cond.column}') {op_map[cond.operator]} {value_str}")

    if len(parts) == 1:
        return parts[0]
    else:
        combinator = ' and ' if conditions[0].combinator == 'and' else ' or '
        return f"({combinator.join(parts)})"


def _generate_python_row_transform(transform: InferredTransformation) -> Tuple[str, str]:
    """Generate Python row transformation code."""
    lines = []
    lines.append("result = {{}")

    for t in transform.column_transforms:
        if t.transform_type == 'drop':
            continue  # Dropped columns don't appear in output
        elif t.transform_type == 'identity':
            lines.append(f"    result['{t.output_column}'] = row.get('{t.input_column}')")
        elif t.transform_type == 'rename':
            lines.append(f"    result['{t.output_column}'] = row.get('{t.input_column}')")
        elif t.transform_type == 'copy':
            lines.append(f"    result['{t.output_column}'] = row.get('{t.input_column}')")
        elif t.transform_type == 'constant':
            val = _format_value_for_code(t.transform_params.get('value'), 'python')
            lines.append(f"    result['{t.output_column}'] = {val}")
        elif t.transform_type == 'linear':
            a = t.transform_params['a']
            b = t.transform_params['b']
            lines.append(f"    result['{t.output_column}'] = {a} * row.get('{t.input_column}') + {b}")
        elif t.transform_type == 'string_lower':
            lines.append(f"    result['{t.output_column}'] = row.get('{t.input_column}').lower() if row.get('{t.input_column}') is not None else None")
        elif t.transform_type == 'string_upper':
            lines.append(f"    result['{t.output_column}'] = row.get('{t.input_column}').upper() if row.get('{t.input_column}') is not None else None")
        elif t.transform_type == 'string_trim':
            lines.append(f"    result['{t.output_column}'] = row.get('{t.input_column}').strip() if row.get('{t.input_column}') is not None else None")
        elif t.transform_type == 'string_prefix':
            prefix = t.transform_params.get('prefix', '')
            lines.append(f"    result['{t.output_column}'] = '{prefix}' + row.get('{t.input_column}') if row.get('{t.input_column}') is not None else None")
        elif t.transform_type == 'string_suffix':
            suffix = t.transform_params.get('suffix', '')
            lines.append(f"    result['{t.output_column}'] = row.get('{t.input_column}') + '{suffix}' if row.get('{t.input_column}') is not None else None")

    # Add stateful transform computations
    for st in transform.stateful_transforms:
        if hasattr(st, 'transform_subtype') and st.transform_subtype == 'prefix_sum':
            lines.append(f"    result['{st.output_column}'] = self._get_prefix_sum('{st.input_column}')")
        elif hasattr(st, 'transform_subtype') and st.transform_subtype in ('prefix_avg', 'prefix_count'):
            lines.append(f"    result['{st.output_column}'] = self._get_prefix_stat('{st.input_column}', '{st.transform_subtype}')")
        elif hasattr(st, 'transform_subtype') and st.transform_subtype == 'sliding_window':
            lines.append(f"    result['{st.output_column}'] = self._get_sliding_window('{st.input_column}', {st.window_size}, '{st.operation}')")
        elif hasattr(st, 'transform_subtype') and st.transform_subtype == 'state_machine':
            lines.append(f"    result['{st.output_column}'] = self._get_state_machine_state('{st.input_column}', row.get('{st.input_column}'))")

    transform_code = '\n'.join(lines)
    return transform_code, transform_code


def _generate_stateful_init_code(transform: InferredTransformation) -> str:
    """Generate state initialization code for stateful transforms."""
    lines = [
        "",
        "        # Stateful transform state",
        "        self._prefix_sums = {}",
        "        self._prefix_counts = {}",
        "        self._prefix_totals = {}",
        "        self._sliding_windows = {}",
        "        self._state_machines = {}",
        "        self._row_buffer = []  # For neighbor-based filtering lookahead",
    ]

    # Initialize prefix sum targets
    for st in transform.stateful_transforms:
        if hasattr(st, 'transform_subtype') and st.transform_subtype in ('prefix_sum', 'prefix_avg', 'prefix_count'):
            lines.append(f"        self._prefix_sums['{st.input_column}'] = 0.0")
        if hasattr(st, 'transform_subtype') and st.transform_subtype in ('prefix_avg',):
            lines.append(f"        self._prefix_totals['{st.input_column}'] = 0.0")
        if hasattr(st, 'transform_subtype') and st.transform_subtype in ('prefix_count',):
            lines.append(f"        self._prefix_counts['{st.input_column}'] = 0")

    # Initialize sliding windows
    for st in transform.stateful_transforms:
        if hasattr(st, 'transform_subtype') and st.transform_subtype == 'sliding_window':
            lines.append(f"        self._sliding_windows['{st.input_column}_{st.window_size}'] = []")

    # Initialize state machines
    for st in transform.stateful_transforms:
        if hasattr(st, 'transform_subtype') and st.transform_subtype == 'state_machine':
            lines.append(f"        self._state_machines['{st.input_column}'] = {{}}")
            lines.append(f"        self._state_machines['{st.input_column}']['current_state'] = {st.initial_state}")

    return '\n'.join(lines)


def _generate_stateful_methods() -> str:
    """Generate stateful transform method implementations."""
    return '''
    def _update_state(self, row: Dict[str, Any]):
        """Update internal state based on the current row."""
        # Update prefix sums
        for col, total in self._prefix_sums.items():
            val = row.get(col)
            if isinstance(val, (int, float)):
                self._prefix_sums[col] = total + val

        # Update prefix counts and totals
        for col in self._prefix_counts:
            val = row.get(col)
            if isinstance(val, (int, float)):
                self._prefix_counts[col] = self._prefix_counts.get(col, 0) + 1
                self._prefix_totals[col] = self._prefix_totals.get(col, 0.0) + val

        # Update sliding windows
        for key in self._sliding_windows:
            parts = key.split('_')
            if len(parts) >= 2:
                col = '_'.join(parts[:-1])
                try:
                    window_size = int(parts[-1])
                except ValueError:
                    continue
                val = row.get(col)
                if isinstance(val, (int, float)):
                    window = self._sliding_windows[key]
                    window.append(val)
                    if len(window) > window_size:
                        window.pop(0)

        # Update row buffer for neighbor-based filtering
        self._row_buffer.append(row)
        if len(self._row_buffer) > 2:
            self._row_buffer.pop(0)

    def _get_prefix_sum(self, column: str) -> float:
        """Get cumulative sum for the prefix up to current row."""
        return self._prefix_sums.get(column, 0.0)

    def _get_prefix_stat(self, column: str, stat_type: str) -> float:
        """Get prefix statistics (avg, count, etc.)."""
        count = self._prefix_counts.get(column, 0)
        total = self._prefix_totals.get(column, 0.0)

        if stat_type == 'avg':
            return total / count if count > 0 else 0.0
        elif stat_type == 'count':
            return float(count)
        return 0.0

    def _get_sliding_window(self, column: str, window_size: int, operation: str) -> float:
        """Get sliding window aggregate over last W values."""
        key = f"{column}_{window_size}"
        window = self._sliding_windows.get(key, [])

        if not window:
            return 0.0

        if operation == 'sum':
            return sum(window)
        elif operation == 'avg':
            return sum(window) / len(window)
        return 0.0

    def _get_state_machine_state(self, column: str, value: Any) -> int:
        """Get current state of the state machine."""
        sm = self._state_machines.get(column, {})
        current_state = sm.get('current_state', 1)

        # Simple threshold-based transition (for now)
        if value is not None and isinstance(value, (int, float)):
            if value >= 1.0:
                new_state = 2
            else:
                new_state = 1

            sm['current_state'] = new_state
            self._state_machines[column] = sm
            return new_state

        return current_state
'''


def _generate_python_reader_setup(fmt: str) -> str:
    """Generate Python file reading setup code."""
    if fmt == 'csv':
        return '''
        if path.endswith('.csv'):
            with open(path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    # Convert values
                    yield {k: _convert_value(v) for k, v in row.items()}
'''
    elif fmt == 'tsv':
        return '''
        if path.endswith('.tsv'):
            with open(path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f, delimiter='\\t')
                for row in reader:
                    yield {k: _convert_value(v) for k, v in row.items()}
'''
    elif fmt == 'jsonl':
        return '''
        if path.endswith('.jsonl'):
            with open(path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        yield json.loads(line)
'''
    elif fmt == 'json':
        return '''
        if path.endswith('.json'):
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, list):
                    for row in data:
                        yield row
'''
    return ''


def _convert_value(value: Any) -> Any:
    """Convert a value from string to appropriate type."""
    if isinstance(value, str):
        lower = value.lower()
        if lower == 'true':
            return True
        elif lower == 'false':
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


def generate_javascript_module(transform: InferredTransformation, output_dir: str, module_name: str):
    """Generate JavaScript module for the transformation."""
    module_path = Path(output_dir)
    module_path.mkdir(parents=True, exist_ok=True)

    # Generate index.js (main module)
    js_content = _generate_javascript_preprocessor(transform)
    (module_path / f'{module_name}.js').write_text(js_content, encoding='utf-8')


def _generate_javascript_preprocessor(transform: InferredTransformation) -> str:
    """Generate the JavaScript DynamicPreprocessor implementation."""
    filter_code = _generate_javascript_filter(transform.filter_conditions)
    transform_code = _generate_javascript_row_transform(transform)
    reader_setup = _generate_javascript_reader_setup(transform.file_format)

    return f'''/**
 * DynamicPreprocessor implementation for transformed data streaming.
 */

const fs = require('fs');
const path = require('path');

class DynamicPreprocessor {{
    /**
     * Create a DynamicPreprocessor instance.
     * @param {{object}} options - Configuration options
     * @param {{number}} options.buffer - Maximum number of rows to keep in memory at once
     * @param {{string}} [options.cache_dir] - Optional directory for caching and resuming
     */
    constructor(options) {{
        this.buffer = options.buffer || 1024;
        this.cacheDir = options.cache_dir || null;
        this.inputColumns = {transform.input_columns};
        this.outputColumns = {transform.output_columns};
        this.fileFormat = '{transform.file_format}';

        if (this.cacheDir) {{
            if (!fs.existsSync(this.cacheDir)) {{
                fs.mkdirSync(this.cacheDir, {{ recursive: true }});
            }}
        }}
    }}

    /**
     * Process a data file and return an iterable of transformed rows.
     * @param {{string}} filePath - Path to the data file
     * @returns {{Iterator}} Iterator over transformed rows
     */
    *{{{filePath}}}) {{
        const rows = yield* this._processFile(filePath);
        return rows;
    }}

    *_processFile(filePath) {{
        const pathObj = path.parse(filePath);
        let cacheFile = null;
        let resumeFrom = 0;

        // Setup cache if enabled
        if (this.cacheDir) {{
            const cacheKey = pathObj.name.replace(/\\.+/g, '_');
            cacheFile = path.join(this.cacheDir, `{{cacheKey}}_cache.json`);
            const indexFile = path.join(this.cacheDir, `{{cacheKey}}_index.txt`);

            // Check for existing cache
            if (fs.existsSync(cacheFile) && fs.existsSync(indexFile)) {{
                resumeFrom = parseInt(fs.readFileSync(indexFile, 'utf8').trim(), 10);

                // Yield cached rows first
                const cachedRows = JSON.parse(fs.readFileSync(cacheFile, 'utf8'));
                for (const row of cachedRows) {{
                    yield row;
                }}
            }}
        }}

        // Process file with streaming
        let rowsBuffer = [];
        let currentIdx = 0;

        for (const rawRow of this._readFile(filePath)) {{
            if (currentIdx < resumeFrom) {{
                currentIdx++;
                continue;
            }}

            // Apply transformation
            const transformed = {transform_code};

            // Apply filter
            if ({filter_code}) {{
                rowsBuffer.push(transformed);

                if (rowsBuffer.length >= this.buffer) {{
                    // Yield buffered rows and update cache
                    for (const row of rowsBuffer) {{
                        yield row;
                    }}

                    if (cacheFile) {{
                        this._updateCache(cacheFile, rowsBuffer);
                    }}

                    rowsBuffer = [];
                }}
            }}

            currentIdx++;
        }}

        // Yield remaining rows
        for (const row of rowsBuffer) {{
            yield row;
        }}

        // Update cache with final rows
        if (cacheFile && rowsBuffer.length > 0) {{
            this._updateCache(cacheFile, rowsBuffer);
        }}

        // Update index
        if (this.cacheDir && cacheFile) {{
            const indexFile = path.join(this.cacheDir, `{{path.basename(cacheFile, '_cache.json')}}_index.txt`);
            fs.writeFileSync(indexFile, currentIdx.toString());
        }}
    }}

    *_readFile(filePath) {{
        {reader_setup}
    }}

    _updateCache(cacheFile, rows) {{
        let existing = [];
        if (fs.existsSync(cacheFile)) {{
            existing = JSON.parse(fs.readFileSync(cacheFile, 'utf8'));
        }}
        existing.push(...rows);
        fs.writeFileSync(cacheFile, JSON.stringify(existing));
    }}
}}

module.exports.DynamicPreprocessor = DynamicPreprocessor;
'''


def _generate_javascript_filter(conditions: List[FilterCondition]) -> str:
    """Generate JavaScript filter code."""
    if not conditions:
        return 'true'

    op_map = {
        'eq': '===',
        'ne': '!==',
        'lt': '<',
        'le': '<=',
        'gt': '>',
        'ge': '>='
    }

    parts = []
    for cond in conditions:
        value_str = _format_value_for_code(cond.value, 'javascript')
        parts.append(f"row['{cond.column}'] {op_map[cond.operator]} {value_str}")

    if len(parts) == 1:
        return parts[0]
    else:
        combinator = ' && ' if conditions[0].combinator == 'and' else ' || '
        return f"({combinator.join(parts)})"


def _generate_javascript_row_transform(transform: InferredTransformation) -> str:
    """Generate JavaScript row transformation code."""
    lines = []
    lines.append("const result = {{};")

    for t in transform.column_transforms:
        if t.transform_type == 'drop':
            continue
        elif t.transform_type == 'identity':
            lines.append(f"    result['{t.output_column}'] = rawRow['{t.input_column}'];")
        elif t.transform_type in ('rename', 'copy'):
            lines.append(f"    result['{t.output_column}'] = rawRow['{t.input_column}'];")
        elif t.transform_type == 'constant':
            val = _format_value_for_code(t.transform_params.get('value'), 'javascript')
            lines.append(f"    result['{t.output_column}'] = {val};")
        elif t.transform_type == 'linear':
            a = t.transform_params['a']
            b = t.transform_params['b']
            lines.append(f"    result['{t.output_column}'] = {a} * rawRow['{t.input_column}'] + {b};")
        elif t.transform_type == 'string_lower':
            lines.append(f"    result['{t.output_column}'] = rawRow['{t.input_column}'] ? rawRow['{t.input_column}'].toLowerCase() : null;")
        elif t.transform_type == 'string_upper':
            lines.append(f"    result['{t.output_column}'] = rawRow['{t.input_column}'] ? rawRow['{t.input_column}'].toUpperCase() : null;")
        elif t.transform_type == 'string_trim':
            lines.append(f"    result['{t.output_column}'] = rawRow['{t.input_column}'] ? rawRow['{t.input_column}'].trim() : null;")
        elif t.transform_type == 'string_prefix':
            prefix = t.transform_params.get('prefix', '')
            lines.append(f"    result['{t.output_column}'] = rawRow['{t.input_column}'] ? '{prefix}' + rawRow['{t.input_column}'] : null;")
        elif t.transform_type == 'string_suffix':
            suffix = t.transform_params.get('suffix', '')
            lines.append(f"    result['{t.output_column}'] = rawRow['{t.input_column}'] ? rawRow['{t.input_column}'] + '{suffix}' : null;")

    return '\n'.join(lines)


def _generate_javascript_reader_setup(fmt: str) -> str:
    """Generate JavaScript file reading setup code."""
    if fmt == 'csv':
        return '''
        if (filePath.endsWith('.csv')) {
            const content = fs.readFileSync(filePath, 'utf8');
            const lines = content.split('\\n');
            if (lines.length > 0) {
                const headers = lines[0].split(',');
                for (let i = 1; i < lines.length; i++) {
                    if (lines[i].trim()) {
                        const values = lines[i].split(',');
                        const row = {};
                        headers.forEach((h, idx) => {
                            row[h.trim()] = _convertValue(values[idx] ? values[idx].trim() : '');
                        });
                        yield row;
                    }
                }
            }
        }'''
    elif fmt == 'tsv':
        return '''
        if (filePath.endsWith('.tsv')) {
            const content = fs.readFileSync(filePath, 'utf8');
            const lines = content.split('\\n');
            if (lines.length > 0) {
                const headers = lines[0].split('\\t');
                for (let i = 1; i < lines.length; i++) {
                    if (lines[i].trim()) {
                        const values = lines[i].split('\\t');
                        const row = {};
                        headers.forEach((h, idx) => {
                            row[h.trim()] = _convertValue(values[idx] ? values[idx].trim() : '');
                        });
                        yield row;
                    }
                }
            }
        }'''
    elif fmt == 'jsonl':
        return '''
        if (filePath.endsWith('.jsonl')) {
            const content = fs.readFileSync(filePath, 'utf8');
            const lines = content.split('\\n');
            for (const line of lines) {
                const trimmed = line.trim();
                if (trimmed) {
                    yield JSON.parse(trimmed);
                }
            }
        }'''
    elif fmt == 'json':
        return '''
        if (filePath.endsWith('.json')) {
            const content = fs.readFileSync(filePath, 'utf8');
            const data = JSON.parse(content);
            if (Array.isArray(data)) {
                for (const row of data) {
                    yield row;
                }
            }
        }'''
    return ''


# ============================================================================
# Main
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='Generate a DynamicPreprocessor module from sample data.'
    )
    parser.add_argument(
        'module_name',
        help='Name of the generated module/package'
    )
    parser.add_argument(
        '--output',
        required=True,
        help='Directory where the generated module will be written'
    )
    parser.add_argument(
        '--sample',
        required=True,
        help='Directory containing input.* and output.* sample files'
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        '--python',
        action='store_true',
        help='Generate Python module'
    )
    group.add_argument(
        '--javascript',
        action='store_true',
        help='Generate JavaScript module'
    )

    args = parser.parse_args()

    # Validate sample directory
    sample_dir = Path(args.sample)
    if not sample_dir.exists():
        print(f"Error: Sample directory '{args.sample}' does not exist", file=sys.stderr)
        sys.exit(1)

    # Find input and output files
    input_files = list(sample_dir.glob('input.*'))
    output_files = list(sample_dir.glob('output.*'))

    if len(input_files) != 1 or len(output_files) != 1:
        print(f"Error: Sample directory must contain exactly one 'input.*' and one 'output.*' file", file=sys.stderr)
        sys.exit(1)

    input_file = input_files[0]
    output_file = output_files[0]

    # Detect format
    fmt = detect_format(input_file)

    # Read sample data
    input_rows = read_sample_data(str(input_file), fmt)
    output_rows = read_sample_data(str(output_file), fmt)

    if len(input_rows) == 0:
        print("Error: Input sample file is empty", file=sys.stderr)
        sys.exit(1)

    # Infer transformation
    transform = infer_transformation(input_rows, output_rows, fmt)

    # Generate module
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.python:
        generate_python_module(transform, str(output_dir), args.module_name)
    elif args.javascript:
        generate_javascript_module(transform, str(output_dir), args.module_name)

    print(f"Generated module '{args.module_name}' in {output_dir}")


if __name__ == '__main__':
    main()
