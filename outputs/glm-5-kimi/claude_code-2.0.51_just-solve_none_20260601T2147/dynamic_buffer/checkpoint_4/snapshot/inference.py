#!/usr/bin/env python3
"""Transformation inference from input/output sample data.

Supports inference of:
- Simple transforms (identity, string ops, linear)
- Filtering conditions
- Stateful transforms (prefix sum, prefix count, sliding window)
- Advanced window functions (centered windows, median, partitions)
- Ranking functions (row_number, rank, dense_rank)
- Complex state resets (segment-based, multi-column)
"""

from typing import Any, Dict, List, Optional, Tuple, Set
import math


def fit_linear(x_vals: List, y_vals: List, tolerance: float = 1e-9) -> Optional[Tuple[float, float]]:
    """Fit linear relationship y = a*x + b. Returns (a, b) or None if no fit."""
    if len(x_vals) < 2 or x_vals[0] == x_vals[1]:
        return None
    a = (y_vals[1] - y_vals[0]) / (x_vals[1] - x_vals[0])
    b = y_vals[0] - a * x_vals[0]
    if any(abs(a * x_vals[i] + b - y_vals[i]) > tolerance for i in range(len(y_vals))):
        return None
    return (a, b)


def compute_median(values: List[float]) -> float:
    """Compute median using lower-middle rule for even count.

    For an odd number of values: middle value in sorted order.
    For an even number: element at index floor((k-1)/2) in 0-based sorted order.
    """
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    k = len(sorted_vals)
    idx = (k - 1) // 2
    return sorted_vals[idx]


class TransformationInferrer:
    """Infers transformations from input/output sample data."""

    def __init__(self, input_rows: List[Dict[str, Any]], output_rows: List[Dict[str, Any]]):
        self.input_rows = input_rows
        self.output_rows = output_rows
        self.input_cols = list(input_rows[0].keys()) if input_rows else []
        self.output_cols = list(output_rows[0].keys()) if output_rows else []
        self.column_transforms: Dict[str, Dict[str, Any]] = {}
        self.filter_conditions: List[Dict[str, Any]] = []
        self.row_mapping: Dict[int, int] = {}
        self.stateful_transforms: List[Dict[str, Any]] = []
        self.neighbor_filters: List[Dict[str, Any]] = []
        self.partition_columns: List[str] = []
        self.segment_columns: List[str] = []

    def infer(self) -> Dict[str, Any]:
        self._infer_row_mapping()
        self._infer_partition_columns()
        self._infer_filter_conditions()
        self._infer_column_transforms()
        return {
            'column_transforms': self.column_transforms,
            'filter_conditions': self.filter_conditions,
            'input_columns': self.input_cols,
            'output_columns': self.output_cols,
            'row_mapping': self.row_mapping,
            'stateful_transforms': self.stateful_transforms,
            'neighbor_filters': self.neighbor_filters,
            'partition_columns': self.partition_columns,
        }

    def _infer_partition_columns(self):
        """Detect partition columns for grouped windows.

        Partition columns are those that:
        1. Exist in both input and output
        2. Define natural groupings for window/ranking operations
        """
        # Try to identify partition columns by looking for repeated value sequences
        # that would indicate grouping
        candidate_partitions = []

        for col in self.input_cols:
            if col in self.output_cols:
                # Check if values in this column form natural groupings
                values = [row.get(col) for row in self.output_rows]
                # Count unique values
                unique_vals = set(v for v in values if v is not None)

                # Heuristic: if there are repeated values and not too many unique
                if 1 < len(unique_vals) <= 1024 and len(values) > len(unique_vals):
                    candidate_partitions.append((col, len(unique_vals)))

        # Sort by number of groups (fewer groups = more likely partition column)
        candidate_partitions.sort(key=lambda x: x[1])

        # Take at most 2 partition columns
        self.partition_columns = [c[0] for c in candidate_partitions[:2]]

    def _infer_row_mapping(self):
        used_input_indices = set()
        for out_idx, out_row in enumerate(self.output_rows):
            best_score = -1
            best_in_idx = None
            for in_idx, in_row in enumerate(self.input_rows):
                if in_idx in used_input_indices:
                    continue
                score = self._row_match_score(in_row, out_row)
                if score > best_score:
                    best_score = score
                    best_in_idx = in_idx
            if best_in_idx is not None and best_score > 0:
                self.row_mapping[out_idx] = best_in_idx
                used_input_indices.add(best_in_idx)

    def _row_match_score(self, input_row: Dict[str, Any], output_row: Dict[str, Any]) -> int:
        score = 0
        for out_col, out_val in output_row.items():
            if out_col in input_row:
                if input_row[out_col] == out_val:
                    score += 10
                elif self._check_transform_match(input_row, out_col, out_val):
                    score += 3
                elif isinstance(input_row[out_col], (int, float)) and isinstance(out_val, (int, float)):
                    score += 1
                else:
                    score -= 5
            else:
                if any(in_val == out_val for in_val in input_row.values()):
                    score += 2
                else:
                    score -= 2
        return score

    def _check_transform_match(self, input_row: Dict[str, Any], col: str, out_val: Any) -> bool:
        in_val = input_row.get(col)
        if in_val is None:
            return False
        if isinstance(in_val, str) and isinstance(out_val, str):
            return (in_val.strip() == out_val or in_val.lower() == out_val or
                    in_val.upper() == out_val or in_val.strip().lower() == out_val or
                    in_val.strip().upper() == out_val)
        if isinstance(in_val, (int, float)) and isinstance(out_val, (int, float)):
            return abs(in_val - out_val) < 1e-9
        return False

    def _infer_filter_conditions(self):
        if not self.output_rows:
            return

        kept_input_indices = set(self.row_mapping.values())
        dropped_input_indices = set(range(len(self.input_rows))) - kept_input_indices

        if not dropped_input_indices:
            return

        dropped_rows = [self.input_rows[i] for i in sorted(dropped_input_indices)]
        kept_rows = [self.input_rows[i] for i in sorted(kept_input_indices)]

        self.filter_conditions = self._find_distinguishing_conditions(kept_rows, dropped_rows)

        per_row_explains_all = self.filter_conditions and all(
            any(self._row_matches_condition(row, cond) for cond in self.filter_conditions)
            for row in dropped_rows
        )

        if not per_row_explains_all:
            self.neighbor_filters = self._infer_neighbor_filters(dropped_input_indices, kept_input_indices)

    def _row_matches_condition(self, row: Dict[str, Any], cond: Dict[str, Any]) -> bool:
        col = cond.get('column')
        op = cond.get('operator')
        val = cond.get('value')
        row_val = row.get(col)

        if op == '!=':
            return row_val == val
        if op == '==':
            return row_val != val
        if op == '>' and isinstance(row_val, (int, float)):
            return row_val <= val
        if op == '>=' and isinstance(row_val, (int, float)):
            return row_val < val
        if op == '<' and isinstance(row_val, (int, float)):
            return row_val >= val
        if op == '<=' and isinstance(row_val, (int, float)):
            return row_val > val
        return False

    def _infer_neighbor_filters(self, dropped_indices: Set[int], kept_indices: Set[int]) -> List[Dict[str, Any]]:
        for col in self.input_cols:
            dropped_next_vals = []
            for dropped_idx in dropped_indices:
                next_idx = dropped_idx + 1
                dropped_next_vals.append(
                    self.input_rows[next_idx].get(col) if next_idx < len(self.input_rows) else None
                )

            if None not in dropped_next_vals and len(set(dropped_next_vals)) == 1:
                candidate_val = dropped_next_vals[0]
                if candidate_val is not None:
                    kept_have_pattern = any(
                        next_idx < len(self.input_rows) and
                        self.input_rows[next_idx].get(col) == candidate_val
                        for kept_idx in kept_indices
                        for next_idx in [kept_idx + 1]
                    )
                    if not kept_have_pattern:
                        return [{
                            'type': 'next_row_condition',
                            'column': col,
                            'operator': '==',
                            'value': candidate_val,
                        }]

        for col in self.input_cols:
            def is_consecutive_dup(idx):
                return idx > 0 and self.input_rows[idx].get(col) == self.input_rows[idx - 1].get(col) and self.input_rows[idx].get(col) is not None

            if all(is_consecutive_dup(i) for i in dropped_indices):
                if not any(is_consecutive_dup(i) for i in kept_indices):
                    return [{
                        'type': 'consecutive_duplicate',
                        'column': col,
                    }]

        return []

    def _find_distinguishing_conditions(self, kept_rows: List[Dict], dropped_rows: List[Dict]) -> List[Dict]:
        if not dropped_rows:
            return []
        conditions = []
        for col in self.input_cols:
            dropped_values = [row.get(col) for row in dropped_rows]
            kept_values = [row.get(col) for row in kept_rows]
            eq_conditions = self._check_equality_filter(col, kept_values, dropped_values)
            if eq_conditions:
                conditions.extend(eq_conditions)
                continue
            comp_conditions = self._check_comparison_filter(col, kept_values, dropped_values)
            if comp_conditions:
                conditions.extend(comp_conditions)
        return conditions

    def _check_equality_filter(self, col: str, kept_values: List, dropped_values: List) -> List[Dict]:
        from parsers import parse_value
        unique_dropped = set(str(v) for v in dropped_values if v is not None)
        unique_kept = set(str(v) for v in kept_values if v is not None)
        if len(unique_dropped) == 1:
            val = list(unique_dropped)[0]
            if val not in unique_kept:
                return [{'type': 'equality', 'column': col, 'operator': '!=',
                          'value': parse_value(val)}]
        return []

    def _check_comparison_filter(self, col: str, kept_values: List, dropped_values: List) -> List[Dict]:
        conditions = []
        try:
            dropped_numeric = [float(v) for v in dropped_values if v is not None]
            kept_numeric = [float(v) for v in kept_values if v is not None]
            if dropped_numeric and kept_numeric:
                max_dropped = max(dropped_numeric)
                min_kept = min(kept_numeric)
                if max_dropped < min_kept:
                    threshold = (max_dropped + min_kept) / 2
                    conditions.append({'type': 'comparison', 'column': col, 'operator': '>', 'value': threshold})
        except (ValueError, TypeError):
            pass
        return conditions

    def _infer_column_transforms(self):
        for out_col in self.output_cols:
            transform = self._infer_single_column_transform(out_col)
            self.column_transforms[out_col] = transform

            if transform.get('type') == 'unknown':
                stateful = self._try_infer_stateful_transform(out_col)
                if stateful:
                    self.stateful_transforms.append(stateful)
                    self.column_transforms[out_col] = {
                        'type': 'stateful',
                        'transform_index': len(self.stateful_transforms) - 1,
                    }

    def _infer_single_column_transform(self, out_col: str) -> Dict[str, Any]:
        if not self.output_rows:
            return {'type': 'unknown'}
        out_values = [row.get(out_col) for row in self.output_rows]

        if out_col in self.input_cols and self._column_matches(out_col, out_values):
            return {'type': 'identity', 'source': out_col}

        for in_col in self.input_cols:
            if self._column_matches(in_col, out_values):
                return {'type': 'copy', 'source': in_col}

        if self._check_constant(out_values):
            return {'type': 'constant', 'value': out_values[0]}

        check_cols = ([out_col] if out_col in self.input_cols else []) + \
                     [c for c in self.input_cols if c != out_col]
        for in_col in check_cols:
            for checker in (self._check_string_transform, self._check_numeric_transform):
                transform = checker(in_col, out_values)
                if transform:
                    return transform

        return {'type': 'unknown'}

    def _column_matches(self, col: str, out_values: List) -> bool:
        for out_idx, out_val in enumerate(out_values):
            if out_idx not in self.row_mapping:
                return False
            if self.input_rows[self.row_mapping[out_idx]].get(col) != out_val:
                return False
        return True

    def _check_constant(self, out_values: List) -> bool:
        return bool(out_values) and all(v == out_values[0] for v in out_values)

    def _check_string_transform(self, in_col: str, out_values: List) -> Optional[Dict]:
        if not out_values or not isinstance(out_values[0], str):
            return None
        in_values = []
        for out_idx in range(len(out_values)):
            if out_idx not in self.row_mapping:
                return None
            in_val = self.input_rows[self.row_mapping[out_idx]].get(in_col)
            if not isinstance(in_val, str):
                return None
            in_values.append(in_val)

        n = len(out_values)
        if all(in_values[i].strip() == out_values[i] for i in range(n)):
            if any(in_values[i] != in_values[i].strip() for i in range(n)):
                return {'type': 'strip', 'source': in_col}
        if all(in_values[i].lower() == out_values[i] for i in range(n)):
            return {'type': 'lower', 'source': in_col}
        if all(in_values[i].upper() == out_values[i] for i in range(n)):
            return {'type': 'upper', 'source': in_col}
        if all(in_values[i].strip().lower() == out_values[i] for i in range(n)):
            return {'type': 'strip_lower', 'source': in_col}
        if all(in_values[i].strip().upper() == out_values[i] for i in range(n)):
            return {'type': 'strip_upper', 'source': in_col}

        first_in, first_out = in_values[0], out_values[0]
        if first_out.startswith(first_in):
            suffix = first_out[len(first_in):]
            if all(out_values[i] == in_values[i] + suffix for i in range(n)):
                return {'type': 'add_suffix', 'source': in_col, 'suffix': suffix}
        if first_out.endswith(first_in):
            prefix = first_out[:-len(first_in)]
            if all(out_values[i] == prefix + in_values[i] for i in range(n)):
                return {'type': 'add_prefix', 'source': in_col, 'prefix': prefix}
        return None

    def _check_numeric_transform(self, in_col: str, out_values: List) -> Optional[Dict]:
        if len(out_values) < 2:
            return None
        in_values = []
        for out_idx, out_val in enumerate(out_values):
            if out_idx not in self.row_mapping:
                return None
            in_val = self.input_rows[self.row_mapping[out_idx]].get(in_col)
            if not isinstance(in_val, (int, float)) or not isinstance(out_val, (int, float)):
                return None
            in_values.append(in_val)

        result = fit_linear(in_values, out_values)
        if result:
            return {'type': 'linear', 'source': in_col, 'a': result[0], 'b': result[1]}
        return None

    def _try_infer_stateful_transform(self, out_col: str) -> Optional[Dict[str, Any]]:
        if not self.output_rows:
            return None

        out_values = [row.get(out_col) for row in self.output_rows]

        # Try row_number (simple counter)
        row_num = self._try_row_number(out_col, out_values)
        if row_num:
            return row_num

        # Try prefix sum
        prefix_sum = self._try_prefix_sum(out_col, out_values)
        if prefix_sum:
            return prefix_sum

        # Try prefix count
        prefix_count = self._try_prefix_count(out_col, out_values)
        if prefix_count:
            return prefix_count

        # Try trailing sliding window
        window_transform = self._try_sliding_window(out_col, out_values)
        if window_transform:
            return window_transform

        # Try centered/symmetric window
        centered_window = self._try_centered_window(out_col, out_values)
        if centered_window:
            return centered_window

        # Try ranking functions
        rank_transform = self._try_ranking_transform(out_col, out_values)
        if rank_transform:
            return rank_transform

        # Try state machine
        state_machine = self._try_state_machine(out_col, out_values)
        if state_machine:
            return state_machine

        # Try partitioned window functions
        partitioned = self._try_partitioned_window(out_col, out_values)
        if partitioned:
            return partitioned

        # Try segment-based transform
        segment_transform = self._try_segment_transform(out_col, out_values)
        if segment_transform:
            return segment_transform

        return None

    def _try_row_number(self, out_col: str, out_values: List) -> Optional[Dict[str, Any]]:
        """Detect simple row_number pattern (1, 2, 3, ...)."""
        if not all(isinstance(v, (int, float)) for v in out_values):
            return None

        # Check if values are sequential starting from 1
        if all(abs(out_values[i] - (i + 1)) < 1e-9 for i in range(len(out_values))):
            return {
                'type': 'row_number',
                'output_column': out_col,
                'partition_by': [],
            }

        return None

    def _try_prefix_sum(self, out_col: str, out_values: List) -> Optional[Dict[str, Any]]:
        if len(out_values) < 2 or not all(isinstance(v, (int, float)) for v in out_values):
            return None

        for in_col in self.input_cols:
            in_values = [self.input_rows[self.row_mapping[i]].get(in_col)
                        for i in range(len(out_values)) if i in self.row_mapping]

            if len(in_values) != len(out_values) or not all(isinstance(v, (int, float)) for v in in_values):
                continue

            prefix_sums = []
            running_sum = 0
            for v in in_values:
                running_sum += v
                prefix_sums.append(running_sum)

            result = fit_linear(prefix_sums, out_values, tolerance=1e-6)
            if result:
                return {
                    'type': 'prefix_sum',
                    'source': in_col,
                    'a': result[0],
                    'b': result[1],
                    'output_column': out_col,
                }

        return None

    def _try_prefix_count(self, out_col: str, out_values: List) -> Optional[Dict[str, Any]]:
        if len(out_values) < 2 or not all(isinstance(v, (int, float)) for v in out_values):
            return None

        # First check for simple row_number pattern
        if all(abs(out_values[i] - (i + 1)) < 1e-9 for i in range(len(out_values))):
            return {
                'type': 'prefix_count',
                'source': None,
                'a': 1.0,
                'b': 0.0,
                'output_column': out_col,
            }

        for in_col in self.input_cols:
            in_values = [self.input_rows[self.row_mapping[i]].get(in_col)
                        for i in range(len(out_values)) if i in self.row_mapping]

            if len(in_values) != len(out_values):
                continue

            for condition_type in ['not_null', 'positive', 'negative', 'true']:
                counts = []
                count = 0
                for v in in_values:
                    if self._check_condition(v, condition_type):
                        count += 1
                    counts.append(count)

                result = fit_linear(counts, out_values, tolerance=1e-6)
                if result:
                    return {
                        'type': 'prefix_count',
                        'source': in_col,
                        'condition': condition_type,
                        'a': result[0],
                        'b': result[1],
                        'output_column': out_col,
                    }

        return None

    def _check_condition(self, value: Any, condition_type: str) -> bool:
        if condition_type == 'not_null':
            return value is not None
        if condition_type == 'positive':
            return isinstance(value, (int, float)) and value > 0
        if condition_type == 'negative':
            return isinstance(value, (int, float)) and value < 0
        if condition_type == 'true':
            return value is True
        return False

    def _try_sliding_window(self, out_col: str, out_values: List) -> Optional[Dict[str, Any]]:
        if len(out_values) < 2:
            return None
        if not all(isinstance(v, (int, float)) for v in out_values):
            return None

        max_window = min(17, len(out_values))

        for in_col in self.input_cols:
            in_values = [self.input_rows[self.row_mapping[i]].get(in_col)
                        for i in range(len(out_values)) if i in self.row_mapping]

            if len(in_values) != len(out_values):
                continue

            if not all(isinstance(v, (int, float)) for v in in_values):
                continue

            for window_size in range(1, max_window + 1):
                # Try trailing window sum
                result = self._try_window_op(out_col, in_col, in_values, out_values, window_size, 'sum', 'trailing')
                if result:
                    return result

                # Try trailing window mean
                result = self._try_window_op(out_col, in_col, in_values, out_values, window_size, 'mean', 'trailing')
                if result:
                    return result

                # Try trailing window median
                result = self._try_window_op(out_col, in_col, in_values, out_values, window_size, 'median', 'trailing')
                if result:
                    return result

        return None

    def _try_window_op(self, out_col: str, in_col: str, in_values: List, out_values: List,
                       window_size: int, op: str, window_type: str) -> Optional[Dict[str, Any]]:
        window_values = []
        for i in range(len(in_values)):
            if window_type == 'trailing':
                start = max(0, i - window_size + 1)
                window = in_values[start:i+1]
            else:
                window = in_values[i:min(i+window_size, len(in_values))]

            if op == 'sum':
                window_values.append(sum(window))
            elif op == 'mean':
                window_values.append(sum(window) / len(window) if window else 0.0)
            elif op == 'median':
                window_values.append(compute_median(window))

        result = fit_linear(window_values, out_values, tolerance=1e-6)
        if result:
            return {
                'type': 'sliding_window',
                'source': in_col,
                'window_size': window_size,
                'operation': op,
                'window_type': window_type,
                'a': result[0],
                'b': result[1],
                'output_column': out_col,
            }

        return None

    def _try_centered_window(self, out_col: str, out_values: List) -> Optional[Dict[str, Any]]:
        """Detect centered/symmetric window patterns.

        Window shape: [i - L, ..., i, ..., i + R]
        where L >= 0, R >= 0, and at least one > 0.
        """
        if len(out_values) < 3:
            return None
        if not all(isinstance(v, (int, float)) for v in out_values):
            return None

        max_lookahead = 8
        max_lookbehind = 8

        for in_col in self.input_cols:
            in_values = [self.input_rows[self.row_mapping[i]].get(in_col)
                        for i in range(len(out_values)) if i in self.row_mapping]

            if len(in_values) != len(out_values):
                continue

            if not all(isinstance(v, (int, float)) for v in in_values):
                continue

            # Try different centered window configurations
            for L in range(0, max_lookbehind + 1):
                for R in range(1, max_lookahead + 1):  # R must be > 0 for centered window
                    for op in ['mean', 'median', 'sum']:
                        result = self._try_centered_op(out_col, in_col, in_values, out_values, L, R, op)
                        if result:
                            return result

        return None

    def _try_centered_op(self, out_col: str, in_col: str, in_values: List, out_values: List,
                         L: int, R: int, op: str) -> Optional[Dict[str, Any]]:
        """Try a centered window operation with L lookbehind and R lookahead."""
        window_values = []

        for i in range(len(in_values)):
            start = max(0, i - L)
            end = min(len(in_values), i + R + 1)
            window = in_values[start:end]

            if op == 'sum':
                window_values.append(sum(window))
            elif op == 'mean':
                window_values.append(sum(window) / len(window) if window else 0.0)
            elif op == 'median':
                window_values.append(compute_median(window))

        result = fit_linear(window_values, out_values, tolerance=1e-6)
        if result:
            return {
                'type': 'centered_window',
                'source': in_col,
                'lookbehind': L,
                'lookahead': R,
                'operation': op,
                'a': result[0],
                'b': result[1],
                'output_column': out_col,
            }

        return None

    def _try_ranking_transform(self, out_col: str, out_values: List) -> Optional[Dict[str, Any]]:
        """Detect ranking functions: row_number, rank, dense_rank."""
        if len(out_values) < 2:
            return None

        if not all(isinstance(v, (int, float)) for v in out_values):
            return None

        # Check if there's a ranking pattern based on a source column
        for in_col in self.input_cols:
            in_values = [self.input_rows[self.row_mapping[i]].get(in_col)
                        for i in range(len(out_values)) if i in self.row_mapping]

            if len(in_values) != len(out_values):
                continue

            if not all(isinstance(v, (int, float)) for v in in_values):
                continue

            # Try rank (gaps for ties)
            rank_result = self._check_rank_pattern(in_values, out_values)
            if rank_result:
                return rank_result

            # Try dense_rank (no gaps)
            dense_result = self._check_dense_rank_pattern(in_values, out_values)
            if dense_result:
                return dense_result

        return None

    def _check_rank_pattern(self, in_values: List, out_values: List) -> Optional[Dict[str, Any]]:
        """Check if out_values represents rank of in_values (descending order)."""
        # For ranking, we expect:
        # Higher values get lower ranks (rank 1 = highest value)
        # Ties get the same rank, with gaps after ties

        # Group by output value
        out_groups = {}
        for i, v in enumerate(out_values):
            if v not in out_groups:
                out_groups[v] = []
            out_groups[v].append((i, in_values[i]))

        # Check if groups have same in_values (ties)
        for rank_val, indices in out_groups.items():
            vals = [v for _, v in indices]
            if not all(abs(v - vals[0]) < 1e-9 for v in vals):
                return None

        # Check if ranks are ordered correctly (descending values = ascending ranks)
        sorted_groups = sorted(out_groups.items(), key=lambda x: x[0])
        prev_max_in = float('inf')
        for rank_val, indices in sorted_groups:
            max_in = max(v for _, v in indices)
            if max_in > prev_max_in + 1e-9:  # Higher value should have lower rank
                return None
            prev_max_in = max_in

        # Determine ascending or descending order
        first_group = sorted_groups[0][1]
        first_val = first_group[0][1]
        last_group = sorted_groups[-1][1]
        last_val = last_group[0][1]

        order = 'desc' if first_val > last_val else 'asc'

        return {
            'type': 'rank',
            'source': in_values[0] if in_values else None,
            'order': order,
            'output_column': out_values[0] if out_values else None,
            'partition_by': [],
        }

    def _check_dense_rank_pattern(self, in_values: List, out_values: List) -> Optional[Dict[str, Any]]:
        """Check if out_values represents dense_rank of in_values."""
        # Dense rank: no gaps between ranks
        unique_ranks = sorted(set(out_values))
        if unique_ranks != list(range(1, len(unique_ranks) + 1)):
            return None  # Not dense rank (has gaps)

        # Similar validation as rank
        out_groups = {}
        for i, v in enumerate(out_values):
            if v not in out_groups:
                out_groups[v] = []
            out_groups[v].append((i, in_values[i]))

        for rank_val, indices in out_groups.items():
            vals = [v for _, v in indices]
            if not all(abs(v - vals[0]) < 1e-9 for v in vals):
                return None

        return {
            'type': 'dense_rank',
            'source': in_values[0] if in_values else None,
            'order': 'desc',
            'output_column': out_values[0] if out_values else None,
            'partition_by': [],
        }

    def _try_state_machine(self, out_col: str, out_values: List) -> Optional[Dict[str, Any]]:
        if len(out_values) < 2:
            return None

        unique_values = set(out_values)
        if len(unique_values) > 16:
            return None

        if not all(isinstance(v, int) for v in out_values):
            return None

        state_order = []
        for v in out_values:
            if v not in state_order:
                state_order.append(v)

        if state_order != list(range(min(state_order), max(state_order) + 1)):
            return None

        change_points = []
        for i in range(1, len(out_values)):
            if out_values[i] != out_values[i-1]:
                change_points.append((i, out_values[i-1], out_values[i]))

        if not change_points:
            return None

        best_result = None
        best_score = -1

        for in_col in self.input_cols:
            result = self._find_transition_conditions(in_col, out_values, state_order, change_points)
            if result:
                score = self._score_state_machine(result, in_col, out_values)
                if score > best_score:
                    best_score = score
                    best_result = {
                        'type': 'state_machine',
                        'source': in_col,
                        'initial_state': out_values[0],
                        'transitions': result['transitions'],
                        'output_column': out_col,
                    }

        return best_result

    def _score_state_machine(self, result: Dict, in_col: str, out_values: List) -> int:
        transitions = result.get('transitions', [])
        if not transitions:
            return -1

        in_values = [self.input_rows[self.row_mapping[i]].get(in_col)
                    for i in range(len(out_values)) if i in self.row_mapping]

        if len(in_values) != len(out_values):
            return -1

        state = out_values[0]
        correct = 0
        for i, (in_val, expected_out) in enumerate(zip(in_values, out_values)):
            if isinstance(in_val, (int, float)):
                for t in transitions:
                    threshold = t.get('threshold')
                    target_state = t.get('target_state')
                    if threshold is not None and target_state is not None:
                        if in_val >= threshold and state < target_state:
                            state = target_state

            if state == expected_out:
                correct += 1

        return correct

    def _find_transition_conditions(self, in_col: str, out_values: List,
                                     state_order: List, change_points: List) -> Optional[Dict[str, Any]]:
        transitions = []

        in_values = [self.input_rows[self.row_mapping[i]].get(in_col)
                    for i in range(len(out_values)) if i in self.row_mapping]

        if len(in_values) != len(out_values):
            return None

        for cp_idx, prev_state, new_state in change_points:
            prev_val = in_values[cp_idx - 1]
            curr_val = in_values[cp_idx]

            if not isinstance(prev_val, (int, float)) or not isinstance(curr_val, (int, float)):
                return None

            if prev_val < curr_val:
                threshold = prev_val + (curr_val - prev_val) / 2
                transitions.append({
                    'condition': 'threshold_cross',
                    'threshold': threshold,
                    'target_state': new_state,
                    'direction': 'up',
                })
            elif prev_val > curr_val:
                threshold = curr_val + (prev_val - curr_val) / 2
                transitions.append({
                    'condition': 'threshold_cross',
                    'threshold': threshold,
                    'target_state': new_state,
                    'direction': 'down',
                })
            else:
                return None

        if not transitions:
            return None

        return {'transitions': transitions}

    def _try_partitioned_window(self, out_col: str, out_values: List) -> Optional[Dict[str, Any]]:
        """Detect partitioned window functions.

        These are window functions that operate within groups defined by partition columns.
        """
        if not self.partition_columns or len(out_values) < 2:
            return None

        if not all(isinstance(v, (int, float)) for v in out_values):
            return None

        # Group rows by partition key
        partition_groups = self._get_partition_groups()

        if not partition_groups:
            return None

        # Try to detect window patterns within each partition
        for in_col in self.input_cols:
            # Check if values within each partition follow a pattern
            for window_size in range(1, min(17, len(out_values) + 1)):
                for op in ['sum', 'mean', 'median']:
                    result = self._try_partitioned_window_op(
                        out_col, in_col, out_values, partition_groups, window_size, op
                    )
                    if result:
                        return result

        # Try row_number within partition
        result = self._try_partitioned_row_number(out_col, out_values, partition_groups)
        if result:
            return result

        return None

    def _get_partition_groups(self) -> Dict[tuple, List[int]]:
        """Group output row indices by partition key."""
        groups = {}
        for out_idx in range(len(self.output_rows)):
            key = tuple(self.output_rows[out_idx].get(col) for col in self.partition_columns)
            if key not in groups:
                groups[key] = []
            groups[key].append(out_idx)
        return groups

    def _try_partitioned_window_op(self, out_col: str, in_col: str, out_values: List,
                                    partition_groups: Dict[tuple, List[int]],
                                    window_size: int, op: str) -> Optional[Dict[str, Any]]:
        """Try to match a partitioned window operation."""
        all_window_values = []

        for partition_key, indices in partition_groups.items():
            # Get input values for this partition
            partition_in_vals = []
            for out_idx in indices:
                if out_idx in self.row_mapping:
                    in_idx = self.row_mapping[out_idx]
                    in_val = self.input_rows[in_idx].get(in_col)
                    partition_in_vals.append(in_val)
                else:
                    partition_in_vals.append(None)

            if not all(isinstance(v, (int, float)) for v in partition_in_vals if v is not None):
                continue

            # Compute window values
            for i, out_idx in enumerate(indices):
                start = max(0, i - window_size + 1)
                window = partition_in_vals[start:i+1]
                window = [v for v in window if v is not None]

                if not window:
                    all_window_values.append(0.0)
                elif op == 'sum':
                    all_window_values.append(sum(window))
                elif op == 'mean':
                    all_window_values.append(sum(window) / len(window))
                elif op == 'median':
                    all_window_values.append(compute_median(window))

        if len(all_window_values) != len(out_values):
            return None

        result = fit_linear(all_window_values, out_values, tolerance=1e-6)
        if result:
            return {
                'type': 'partitioned_window',
                'source': in_col,
                'partition_by': self.partition_columns,
                'window_size': window_size,
                'operation': op,
                'a': result[0],
                'b': result[1],
                'output_column': out_col,
            }

        return None

    def _try_partitioned_row_number(self, out_col: str, out_values: List,
                                     partition_groups: Dict[tuple, List[int]]) -> Optional[Dict[str, Any]]:
        """Check if out_values is row_number within partitions."""
        expected = []
        for partition_key, indices in partition_groups.items():
            for i, out_idx in enumerate(indices):
                expected.append(i + 1)

        # Sort by output index to compare
        sorted_expected = [0] * len(out_values)
        for partition_key, indices in partition_groups.items():
            for i, out_idx in enumerate(indices):
                sorted_expected[out_idx] = i + 1

        if all(abs(sorted_expected[i] - out_values[i]) < 1e-9 for i in range(len(out_values))):
            return {
                'type': 'partitioned_row_number',
                'partition_by': self.partition_columns,
                'output_column': out_col,
            }

        return None

    def _try_segment_transform(self, out_col: str, out_values: List) -> Optional[Dict[str, Any]]:
        """Detect segment-based transforms.

        Segments are defined by state resets based on conditions like:
        - Gap exceeding threshold
        - Value change
        - Multiple column conditions
        """
        if len(out_values) < 3:
            return None

        # Try to detect segment_id pattern
        if all(isinstance(v, int) for v in out_values):
            segment_result = self._detect_segment_pattern(out_values)
            if segment_result:
                return segment_result

        return None

    def _detect_segment_pattern(self, out_values: List) -> Optional[Dict[str, Any]]:
        """Detect if output represents segment IDs with reset conditions."""
        # Check for segment pattern: values reset to 1 or increase within segments
        segments = []
        current_segment = []
        current_id = out_values[0]

        for i, v in enumerate(out_values):
            if v < current_id:  # Reset detected
                if current_segment:
                    segments.append(current_segment)
                current_segment = [i]
                current_id = v
            else:
                current_segment.append(i)
                current_id = v

        if current_segment:
            segments.append(current_segment)

        if len(segments) < 2:
            return None

        # Try to find the reset condition
        # Check for gap-based reset
        for in_col in self.input_cols:
            if self._check_gap_reset(in_col, segments, out_values):
                return {
                    'type': 'segment',
                    'source': in_col,
                    'reset_type': 'gap_threshold',
                    'output_column': out_values[0] if out_values else None,
                }

        return None

    def _check_gap_reset(self, in_col: str, segments: List[List[int]], out_values: List) -> bool:
        """Check if segment resets are triggered by gaps in a column."""
        reset_points = [seg[0] for seg in segments[1:]]  # First index of each segment after first

        gaps = []
        for rp in reset_points:
            if rp > 0 and rp - 1 in self.row_mapping and rp in self.row_mapping:
                prev_val = self.input_rows[self.row_mapping[rp - 1]].get(in_col)
                curr_val = self.input_rows[self.row_mapping[rp]].get(in_col)
                if isinstance(prev_val, (int, float)) and isinstance(curr_val, (int, float)):
                    gaps.append(abs(curr_val - prev_val))

        if not gaps:
            return False

        # Check if all gaps are similar (within threshold)
        if len(set(round(g, 2) for g in gaps)) == 1:
            return True

        return False
