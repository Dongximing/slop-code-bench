"""Window function computation engine for SQL analytics."""

import pandas as pd
import numpy as np
from typing import List, Dict, Tuple, Optional, Callable
from enum import Enum
from errors import create_window_error


class FrameType(Enum):
    ROWS = "rows"
    RANGE = "range"


class FrameBound(Enum):
    UNBOUNDED_PRECEDING = "unbounded_preceding"
    UNBOUNDED_FOLLOWING = "unbounded_following"
    CURRENT_ROW = "current_row"


class WindowSpec:
    """Represents a window specification."""

    def __init__(self, function: str, partition_by: List[str], order_by: List[Tuple[str, bool]],
                 frame_clause: Optional[str] = None):
        self.function = function.upper()
        self.partition_by = partition_by
        self.order_by = order_by  # List of (column, ascending) tuples
        self.frame_clause = frame_clause
        self.frame_type = None
        self.frame_start = None
        self.frame_end = None
        self._parse_frame_clause()

    def _parse_frame_clause(self):
        """Parse the frame clause into type and bounds."""
        if not self.frame_clause:
            # Default: RANGE UNBOUNDED PRECEDING
            self.frame_type = FrameType.RANGE
            self.frame_start = FrameBound.UNBOUNDED_PRECEDING
            self.frame_end = FrameBound.CURRENT_ROW
            return

        clause = self.frame_clause.upper()

        if 'ROWS' in clause:
            self.frame_type = FrameType.ROWS
        elif 'RANGE' in clause:
            self.frame_type = FrameType.RANGE

        # Parse bounds
        if 'UNBOUNDED PRECEDING' in clause:
            if self.frame_start is None:
                self.frame_start = FrameBound.UNBOUNDED_PRECEDING
        if 'CURRENT ROW' in clause:
            if self.frame_start is None:
                self.frame_start = FrameBound.CURRENT_ROW
            if self.frame_end is None:
                self.frame_end = FrameBound.CURRENT_ROW
        if 'BETWEEN' in clause:
            parts = clause.split('BETWEEN')[1].split('AND')
            if len(parts) == 2:
                start_str = parts[0].strip()
                end_str = parts[1].strip()

                self.frame_start = self._parse_bound(start_str)
                self.frame_end = self._parse_bound(end_str)

        # Default frame if not BETWEEN
        if self.frame_start is None and self.frame_end is None:
            if 'UNBOUNDED PRECEDING' in clause:
                self.frame_start = FrameBound.UNBOUNDED_PRECEDING
                self.frame_end = FrameBound.CURRENT_ROW
            else:
                # Default
                self.frame_start = FrameBound.UNBOUNDED_PRECEDING
                self.frame_end = FrameBound.CURRENT_ROW

    def _parse_bound(self, bound_str: str) -> FrameBound:
        """Parse a single bound string."""
        bound_upper = bound_str.upper()
        if 'UNBOUNDED PRECEDING' in bound_upper:
            return FrameBound.UNBOUNDED_PRECEDING
        elif 'CURRENT ROW' in bound_upper:
            return FrameBound.CURRENT_ROW
        elif 'UNBOUNDED FOLLOWING' in bound_upper:
            return FrameBound.UNBOUNDED_FOLLOWING
        return FrameBound.CURRENT_ROW


class WindowFunctionError(Exception):
    """Base exception for window function errors."""
    pass


class InvalidWindowSpecError(WindowFunctionError):
    """Invalid window specification."""
    pass


class FrameError(WindowFunctionError):
    """Frame clause error."""
    pass


class NestedWindowError(WindowFunctionError):
    """Nested window functions not supported."""
    pass


def raise_window_error(error_type: str, message: str):
    """Raise a window-related error using the error code system."""
    error = create_window_error(error_type, message)
    raise error


def compute_window_function(df: pd.DataFrame, spec: WindowSpec) -> pd.Series:
    """
    Compute a window function over a DataFrame.

    Args:
        df: Input DataFrame
        spec: Window specification

    Returns:
        Series with window function results aligned to input rows
    """
    if spec.partition_by:
        # Group by partition columns
        group_keys = [c for c in spec.partition_by if c in df.columns]
        if not group_keys:
            # No valid partition columns - treat as single partition
            return _compute_window_for_partition(df, spec)

        grouped = df.groupby(group_keys, group_keys=False, sort=False)

        # Apply window function to each partition
        results = []
        for _, group in grouped:
            result = _compute_window_for_partition(group.reset_index(drop=True), spec)
            results.append(result)

        # Combine results maintaining original order
        result_series = pd.concat(results, ignore_index=True)
        return result_series.reindex(df.index)
    else:
        # No partition - single window
        return _compute_window_for_partition(df, spec)


def _compute_window_for_partition(df: pd.DataFrame, spec: WindowSpec) -> pd.Series:
    """Compute window function for a single partition."""
    if df.empty:
        return pd.Series([], dtype=float)

    func_upper = spec.function

    # Handle ranking functions
    if func_upper in ['ROW_NUMBER', 'RANK', 'DENSE_RANK']:
        return _compute_ranking_function(df, spec)

    # Handle aggregate window functions
    if func_upper in ['SUM', 'AVG', 'MIN', 'MAX', 'COUNT']:
        return _compute_aggregate_window(df, spec)

    raise InvalidWindowSpecError(f"Unsupported window function: {spec.function}")


def _compute_ranking_function(df: pd.DataFrame, spec: WindowSpec) -> pd.Series:
    """Compute ranking window functions (ROW_NUMBER, RANK, DENSE_RANK)."""
    if not spec.order_by:
        # No ORDER BY - all rows get rank 1
        if spec.function == 'ROW_NUMBER':
            return pd.Series(range(1, len(df) + 1), index=df.index)
        elif spec.function == 'RANK':
            return pd.Series(1, index=df.index)
        elif spec.function == 'DENSE_RANK':
            return pd.Series(1, index=df.index)

    # Sort by order_by columns
    sort_cols = [col for col, _ in spec.order_by if col in df.columns]
    ascending = [not desc for _, desc in spec.order_by]

    if sort_cols:
        df_sorted = df.sort_values(by=sort_cols, ascending=ascending, kind='mergesort')
    else:
        df_sorted = df.copy()

    # Compute ranks based on order_by values
    if spec.function == 'ROW_NUMBER':
        # Simple row number
        result = pd.Series(range(1, len(df_sorted) + 1), index=df_sorted.index)
    elif spec.function == 'RANK':
        # Standard rank with gaps
        order_values = _get_order_values(df_sorted, spec.order_by)
        ranks = _compute_standard_ranks(order_values)
        result = pd.Series(ranks, index=df_sorted.index)
    elif spec.function == 'DENSE_RANK':
        # Dense rank without gaps
        order_values = _get_order_values(df_sorted, spec.order_by)
        ranks = _compute_dense_ranks(order_values)
        result = pd.Series(ranks, index=df_sorted.index)

    # Return to original order
    return result.reindex(df.index)


def _get_order_values(df: pd.DataFrame, order_by: List[Tuple[str, bool]]) -> List:
    """Get sorted values for ranking computation."""
    if not order_by:
        return list(range(len(df)))

    cols = [col for col, _ in order_by if col in df.columns]
    if not cols:
        return list(range(len(df)))

    # Convert to tuples for comparison
    ascending = [not desc for _, desc in order_by]
    sort_df = df[cols].sort_values(by=cols, ascending=ascending)
    return list(sort_df.itertuples(index=False, name=None))


def _compute_standard_ranks(values: List) -> List[int]:
    """Compute standard SQL RANK values with gaps."""
    if not values:
        return []

    ranks = []
    current_rank = 1
    for i, val in enumerate(values):
        if i > 0 and val != values[i - 1]:
            current_rank = i + 1
        ranks.append(current_rank)
    return ranks


def _compute_dense_ranks(values: List) -> List[int]:
    """Compute dense rank values without gaps."""
    if not values:
        return []

    ranks = []
    current_rank = 1
    unique_vals = []

    for val in values:
        if not unique_vals or val != unique_vals[-1]:
            unique_vals.append(val)

    val_to_rank = {val: i + 1 for i, val in enumerate(unique_vals)}

    for val in values:
        ranks.append(val_to_rank[val])

    return ranks


def _compute_aggregate_window(df: pd.DataFrame, spec: WindowSpec) -> pd.Series:
    """Compute aggregate window functions (SUM, AVG, MIN, MAX, COUNT)."""
    if not spec.order_by and spec.frame_type != FrameType.ROWS:
        # No ORDER BY - simple aggregate over partition
        return _compute_simple_aggregate(df, spec)

    # With ORDER BY - compute frame-based aggregate
    return _compute_frame_aggregate(df, spec)


def _compute_simple_aggregate(df: pd.DataFrame, spec: WindowSpec) -> pd.Series:
    """Compute aggregate without frame (over entire partition)."""
    col_name = _get_function_column(spec)

    if col_name is None or col_name not in df.columns:
        if spec.function == 'COUNT':
            # COUNT(*) over partition
            return pd.Series(len(df), index=df.index)
        raise InvalidWindowSpecError(f"Column '{col_name}' not found for {spec.function}")

    col = df[col_name]

    if spec.function == 'SUM':
        result = col.sum()
    elif spec.function == 'AVG':
        result = col.mean()
    elif spec.function == 'MIN':
        result = col.min()
    elif spec.function == 'MAX':
        result = col.max()
    elif spec.function == 'COUNT':
        result = col.count()
    else:
        raise InvalidWindowSpecError(f"Unsupported aggregate function: {spec.function}")

    return pd.Series(result, index=df.index)


def _compute_frame_aggregate(df: pd.DataFrame, spec: WindowSpec) -> pd.Series:
    """Compute aggregate with frame specification (running totals, etc.)."""
    sort_cols = [col for col, _ in spec.order_by if col in df.columns]
    ascending = [not desc for _, desc in spec.order_by]

    if sort_cols:
        df_sorted = df.sort_values(by=sort_cols, ascending=ascending, kind='mergesort')
    else:
        df_sorted = df.copy()

    col_name = _get_function_column(spec)

    results = []
    n = len(df_sorted)

    for i in range(n):
        # Determine frame boundaries
        start_idx, end_idx = _get_frame_indices(i, df_sorted, spec)

        if col_name and col_name in df_sorted.columns:
            frame_data = df_sorted.iloc[start_idx:end_idx + 1][col_name]
        else:
            # COUNT(*) - all rows in frame
            frame_data = pd.Series([1] * (end_idx - start_idx + 1))

        if spec.function == 'SUM':
            val = frame_data.sum()
        elif spec.function == 'AVG':
            val = frame_data.mean()
        elif spec.function == 'MIN':
            val = frame_data.min()
        elif spec.function == 'MAX':
            val = frame_data.max()
        elif spec.function == 'COUNT':
            val = len(frame_data)
        else:
            raise InvalidWindowSpecError(f"Unsupported aggregate function: {spec.function}")

        results.append(val)

    result_series = pd.Series(results, index=df_sorted.index)
    return result_series.reindex(df.index)


def _get_frame_indices(current_idx: int, df: pd.DataFrame, spec: WindowSpec) -> Tuple[int, int]:
    """Get frame start and end indices for a given row."""
    n = len(df)

    if spec.frame_type == FrameType.ROWS:
        # ROWS frame uses physical row positions
        start = _resolve_row_bound(spec.frame_start, current_idx, n)
        end = _resolve_row_bound(spec.frame_end, current_idx, n)
    else:
        # RANGE frame uses logical value positions
        # For RANGE, all rows with the same ORDER BY values are in the same peer group
        sort_cols = [col for col, _ in spec.order_by if col in df.columns]
        if not sort_cols:
            start = 0
            end = n - 1
        else:
            # Find all rows with ORDER BY values equal to current row
            current_vals = df.iloc[current_idx][sort_cols]
            peer_group = []
            for i in range(n):
                row_vals = df.iloc[i][sort_cols]
                if _values_equal(row_vals, current_vals):
                    peer_group.append(i)

            if not peer_group:
                start = current_idx
                end = current_idx
            else:
                # For RANGE UNBOUNDED PRECEDING, all rows up to current peer group
                start = 0
                end = peer_group[-1]

    return start, end


def _resolve_row_bound(bound: FrameBound, current_idx: int, n: int) -> int:
    """Resolve a ROWS frame bound to an actual index."""
    if bound == FrameBound.UNBOUNDED_PRECEDING:
        return 0
    elif bound == FrameBound.UNBOUNDED_FOLLOWING:
        return n - 1
    elif bound == FrameBound.CURRENT_ROW:
        return current_idx
    return current_idx


def _values_equal(vals1, vals2) -> bool:
    """Check if two sets of values are equal, handling NaN."""
    if isinstance(vals1, pd.Series):
        vals1 = vals1.values
    if isinstance(vals2, pd.Series):
        vals2 = vals2.values

    if len(vals1) != len(vals2):
        return False

    for v1, v2 in zip(vals1, vals2):
        if pd.isna(v1) and pd.isna(v2):
            continue
        if pd.isna(v1) or pd.isna(v2):
            return False
        if v1 != v2:
            return False

    return True


def _get_function_column(spec: WindowSpec) -> Optional[str]:
    """Extract the column name from the function call."""
    func_expr = spec.function

    # Remove function name
    if '(' in func_expr:
        inner = func_expr.split('(', 1)[1].rsplit(')', 1)[0].strip()
        if inner == '*':
            return None
        # Handle column references (with possible table prefix)
        if '.' in inner:
            return inner.split('.')[-1]
        return inner

    return None


def validate_window_spec(spec: WindowSpec) -> None:
    """Validate a window specification."""
    if not spec.function:
        raise InvalidWindowSpecError("Window function name is required")

    func_upper = spec.function.upper()
    valid_functions = ['SUM', 'AVG', 'MIN', 'MAX', 'COUNT', 'ROW_NUMBER', 'RANK', 'DENSE_RANK']
    if func_upper not in valid_functions:
        raise InvalidWindowSpecError(f"Invalid window function: {spec.function}")

    # Validate frame clause
    if spec.frame_clause:
        if 'ROWS' not in spec.frame_clause.upper() and 'RANGE' not in spec.frame_clause.upper():
            raise FrameError(f"Invalid frame clause: {spec.frame_clause}")