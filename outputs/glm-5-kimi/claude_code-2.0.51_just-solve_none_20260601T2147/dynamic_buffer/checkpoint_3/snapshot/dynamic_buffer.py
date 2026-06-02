#!/usr/bin/env python3
"""
Dynamic Buffer - A code generator that infers transformations from sample data
and generates modules for streaming data processing.

Supports:
- Per-row transforms (identity, copy, constant, string ops, linear)
- Prefix/cumulative transforms (sum, count, average)
- Sliding window transforms (sum, mean, count over window)
- State-machine sequence labeling (segment IDs, phase flags)
- Neighbor-based filtering (lookahead-dependent filters)
"""

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Set


# =============================================================================
# Parsers
# =============================================================================

def parse_csv(filepath: str, delimiter: str = ',') -> List[Dict[str, Any]]:
    """Parse a CSV/TSV file and return list of row dicts."""
    with open(filepath, 'r', encoding='utf-8') as f:
        lines = f.read().strip().split('\n')
    if not lines:
        return []
    headers = lines[0].split(delimiter)
    rows = []
    for line in lines[1:]:
        if not line.strip():
            continue
        values = line.split(delimiter)
        rows.append({h: parse_value(values[i]) if i < len(values) else None
                      for i, h in enumerate(headers)})
    return rows


def parse_tsv(filepath: str) -> List[Dict[str, Any]]:
    return parse_csv(filepath, delimiter='\t')


def parse_jsonl(filepath: str) -> List[Dict[str, Any]]:
    with open(filepath, 'r', encoding='utf-8') as f:
        return [json.loads(line) for line in f if line.strip()]


def parse_json_file(filepath: str) -> List[Dict[str, Any]]:
    with open(filepath, 'r', encoding='utf-8') as f:
        return json.load(f)


def parse_value(value: str) -> Any:
    if value == '':
        return None
    if value.lower() == 'true':
        return True
    if value.lower() == 'false':
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


def get_file_extension(filepath: str) -> str:
    return Path(filepath).suffix.lstrip('.').lower()


def parse_file(filepath: str) -> Tuple[List[Dict[str, Any]], str]:
    """Parse a data file and return rows and extension."""
    ext = get_file_extension(filepath)
    parsers = {
        'csv': parse_csv,
        'tsv': parse_tsv,
        'jsonl': parse_jsonl,
        'json': parse_json_file,
    }
    if ext not in parsers:
        raise ValueError(f"Unsupported file extension: {ext}")
    return parsers[ext](filepath), ext


# =============================================================================
# Transformation Inferrer
# =============================================================================

def _fit_linear(x_vals: List, y_vals: List, tolerance: float = 1e-9) -> Optional[Tuple[float, float]]:
    """Fit linear relationship y = a*x + b. Returns (a, b) or None if no fit."""
    if len(x_vals) < 2 or x_vals[0] == x_vals[1]:
        return None
    a = (y_vals[1] - y_vals[0]) / (x_vals[1] - x_vals[0])
    b = y_vals[0] - a * x_vals[0]
    if any(abs(a * x_vals[i] + b - y_vals[i]) > tolerance for i in range(len(y_vals))):
        return None
    return (a, b)


class TransformationInferrer:
    def __init__(self, input_rows: List[Dict[str, Any]], output_rows: List[Dict[str, Any]]):
        self.input_rows = input_rows
        self.output_rows = output_rows
        self.input_cols = list(input_rows[0].keys()) if input_rows else []
        self.output_cols = list(output_rows[0].keys()) if output_rows else []
        self.column_transforms: Dict[str, Dict[str, Any]] = {}
        self.filter_conditions: List[Dict[str, Any]] = []
        self.row_mapping: Dict[int, int] = {}  # output_idx -> input_idx
        self.stateful_transforms: List[Dict[str, Any]] = []
        self.neighbor_filters: List[Dict[str, Any]] = []

    def infer(self) -> Dict[str, Any]:
        self._infer_row_mapping()
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
        }

    def _infer_row_mapping(self):
        """Map output rows to input rows based on best match."""
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
        """Score how well an input row matches an output row."""
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
                min_dropped = min(dropped_numeric)
                max_dropped2 = max(dropped_numeric)
                if min_dropped < min_kept and max_dropped2 < min_kept:
                    conditions.append({'type': 'comparison', 'column': col, 'operator': '>=', 'value': min_kept})
        except (ValueError, TypeError):
            pass
        return conditions

    def _infer_column_transforms(self):
        """Infer column transforms including stateful ones."""
        for out_col in self.output_cols:
            transform = self._infer_single_column_transform(out_col)
            self.column_transforms[out_col] = transform

            # Check for stateful transforms
            if transform.get('type') == 'unknown':
                stateful = self._try_infer_stateful_transform(out_col)
                if stateful:
                    self.stateful_transforms.append(stateful)
                    # Mark the column as stateful
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

        # Check string/numeric transforms, preferring same-named column first
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

        result = _fit_linear(in_values, out_values)
        if result:
            return {'type': 'linear', 'source': in_col, 'a': result[0], 'b': result[1]}
        return None

    def _try_infer_stateful_transform(self, out_col: str) -> Optional[Dict[str, Any]]:
        """Try to infer a stateful transform for the output column."""
        if not self.output_rows:
            return None

        out_values = [row.get(out_col) for row in self.output_rows]

        # Try prefix sum transform
        prefix_sum = self._try_prefix_sum(out_col, out_values)
        if prefix_sum:
            return prefix_sum

        # Try prefix count transform
        prefix_count = self._try_prefix_count(out_col, out_values)
        if prefix_count:
            return prefix_count

        # Try sliding window transform
        window_transform = self._try_sliding_window(out_col, out_values)
        if window_transform:
            return window_transform

        # Try state machine transform
        state_machine = self._try_state_machine(out_col, out_values)
        if state_machine:
            return state_machine

        return None

    def _try_prefix_sum(self, out_col: str, out_values: List) -> Optional[Dict[str, Any]]:
        """Try to detect prefix sum pattern: out[i] = a * sum(x[0:i+1]) + b."""
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

            result = _fit_linear(prefix_sums, out_values, tolerance=1e-6)
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
        """Try to detect prefix count pattern: out[i] = count up to and including i."""
        if len(out_values) < 2 or not all(isinstance(v, (int, float)) for v in out_values):
            return None

        # Check if out_values = row_index + 1 (simple row count)
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

                result = _fit_linear(counts, out_values, tolerance=1e-6)
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
        """Try to detect sliding window patterns: sum/mean/count over last W rows."""
        if len(out_values) < 2:
            return None
        if not all(isinstance(v, (int, float)) for v in out_values):
            return None

        # Try window sizes from 2 to min(64, len(out_values))
        max_window = min(64, len(out_values))

        for in_col in self.input_cols:
            in_values = [self.input_rows[self.row_mapping[i]].get(in_col)
                        for i in range(len(out_values)) if i in self.row_mapping]

            if len(in_values) != len(out_values):
                continue

            if not all(isinstance(v, (int, float)) for v in in_values):
                continue

            for window_size in range(1, max_window + 1):
                # Try sum
                result = self._try_window_op(out_col, in_col, in_values, out_values, window_size, 'sum')
                if result:
                    return result

                # Try mean
                result = self._try_window_op(out_col, in_col, in_values, out_values, window_size, 'mean')
                if result:
                    return result

        return None

    def _try_window_op(self, out_col: str, in_col: str, in_values: List, out_values: List,
                       window_size: int, op: str) -> Optional[Dict[str, Any]]:
        window_values = []
        for i in range(len(in_values)):
            start = max(0, i - window_size + 1)
            window = in_values[start:i+1]
            window_values.append(sum(window) if op == 'sum' else sum(window) / len(window))

        result = _fit_linear(window_values, out_values, tolerance=1e-6)
        if result:
            return {
                'type': 'sliding_window',
                'source': in_col,
                'window_size': window_size,
                'operation': op,
                'a': result[0],
                'b': result[1],
                'output_column': out_col,
            }

        return None

    def _try_state_machine(self, out_col: str, out_values: List) -> Optional[Dict[str, Any]]:
        """Try to detect state machine patterns (segment IDs, phase flags)."""
        if len(out_values) < 2:
            return None

        # Check if values are discrete (strings or small integers)
        unique_values = set(out_values)
        if len(unique_values) > 16:
            return None  # Too many states

        # Check if values increment at certain points (segment ID pattern)
        if not all(isinstance(v, int) for v in out_values):
            return None

        # Find the unique output states in order of first appearance
        state_order = []
        for v in out_values:
            if v not in state_order:
                state_order.append(v)

        # Check if states are sequential integers (1,2,3,...)
        if state_order != list(range(min(state_order), max(state_order) + 1)):
            return None

        # Find change points (where output state changes)
        change_points = []
        for i in range(1, len(out_values)):
            if out_values[i] != out_values[i-1]:
                change_points.append((i, out_values[i-1], out_values[i]))

        if not change_points:
            return None  # No state changes detected

        # Try each input column to find which one best explains the transitions
        best_result = None
        best_score = -1

        for in_col in self.input_cols:
            result = self._find_transition_conditions(in_col, out_values, state_order, change_points)
            if result:
                # Score by how well the thresholds would reproduce the output
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
        """Score how well a state machine config reproduces the output."""
        transitions = result.get('transitions', [])
        if not transitions:
            return -1

        # Get input values
        in_values = [self.input_rows[self.row_mapping[i]].get(in_col)
                    for i in range(len(out_values)) if i in self.row_mapping]

        if len(in_values) != len(out_values):
            return -1

        # Simulate the state machine
        state = out_values[0]
        correct = 0
        for i, (in_val, expected_out) in enumerate(zip(in_values, out_values)):
            # Check transitions
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
        """Find conditions that trigger state transitions."""
        transitions = []

        # Get input values for the mapped rows
        in_values = [self.input_rows[self.row_mapping[i]].get(in_col)
                    for i in range(len(out_values)) if i in self.row_mapping]

        if len(in_values) != len(out_values):
            return None

        # For each change point, find the threshold that triggers the transition
        for cp_idx, prev_state, new_state in change_points:
            prev_val = in_values[cp_idx - 1]
            curr_val = in_values[cp_idx]

            if not isinstance(prev_val, (int, float)) or not isinstance(curr_val, (int, float)):
                return None

            # Threshold is between prev and curr value
            if prev_val < curr_val:
                # Crossing above - threshold should trigger new_state when val >= threshold
                threshold = prev_val + (curr_val - prev_val) / 2
                transitions.append({
                    'condition': 'threshold_cross',
                    'threshold': threshold,
                    'target_state': new_state,
                    'direction': 'up',
                })
            elif prev_val > curr_val:
                # Crossing below
                threshold = curr_val + (prev_val - curr_val) / 2
                transitions.append({
                    'condition': 'threshold_cross',
                    'threshold': threshold,
                    'target_state': new_state,
                    'direction': 'down',
                })
            else:
                return None  # Values are equal, can't determine threshold

        if not transitions:
            return None

        return {'transitions': transitions}


# =============================================================================
# Code Generation Helpers
# =============================================================================

def _val_str(val: Any, lang: str = 'py') -> str:
    """Format a value as a string for Python or JavaScript."""
    if isinstance(val, bool):
        return ('True' if val else 'False') if lang == 'py' else ('true' if val else 'false')
    if isinstance(val, str):
        return "'" + val + "'"
    return str(val)


# =============================================================================
# Python Code Generator
# =============================================================================

class PythonCodeGenerator:
    """Generates Python module code from transformation config."""

    def __init__(self, module_name: str, config: Dict, file_ext: str):
        self.module_name = module_name
        self.config = config
        self.file_ext = file_ext

    def generate(self) -> Dict[str, str]:
        return {
            '__init__.py': self._generate_init(),
            'preprocessor.py': self._generate_preprocessor(),
        }

    def _generate_init(self) -> str:
        return '\n'.join([
            'from .preprocessor import DynamicPreprocessor',
            '',
            "__all__ = ['DynamicPreprocessor']",
            '',
        ])

    def _generate_preprocessor(self) -> str:
        delimiter = ',' if self.file_ext == 'csv' else '\t' if self.file_ext == 'tsv' else None
        has_stateful = bool(self.config.get('stateful_transforms'))
        has_neighbor_filters = bool(self.config.get('neighbor_filters'))

        lines = [
            '"""',
            'Dynamic Preprocessor - Streaming data processor with caching support.',
            'Supports stateful transforms and neighbor-based filtering.',
            '"""',
            '',
            'import hashlib',
            'import json',
            'import os',
            'from collections import deque',
            'from pathlib import Path',
            'from typing import Any, Dict, Iterator, List, Optional',
            '',
            '',
            'class DynamicPreprocessor:',
            '    """Streaming preprocessor with caching and resuming support."""',
            '',
            '    def __init__(self, buffer: int, cache_dir: Optional[str] = None):',
            '        self.buffer = buffer',
            '        self.cache_dir = cache_dir',
        ]

        # Add state initialization for stateful transforms
        if has_stateful:
            lines.extend(self._generate_state_init())

        lines.extend([
            '',
            '    def _get_cache_path(self, input_path: str) -> Optional[Path]:',
            '        if self.cache_dir is None:',
            '            return None',
            '        path_hash = hashlib.md5(os.path.abspath(input_path).encode()).hexdigest()',
            '        cache_dir = Path(self.cache_dir)',
            '        cache_dir.mkdir(parents=True, exist_ok=True)',
            '        return cache_dir / f"{path_hash}.json"',
            '',
            '    def _load_cache(self, cache_path: Optional[Path]) -> Dict:',
            '        if cache_path is None or not cache_path.exists():',
            '            return {"processed_rows": 0, "rows": [], "state": {}}',
            "        with open(cache_path, 'r', encoding='utf-8') as f:",
            '            data = json.load(f)',
            '            # Restore state from cache',
            '            if "state" in data:',
            '                self._restore_state(data["state"])',
            '            return data',
            '',
            '    def _save_cache(self, cache_path: Optional[Path], state: Dict):',
            '        if cache_path is None:',
            '            return',
            '        # Include current state in cache',
            '        state["state"] = self._get_current_state()',
            "        with open(cache_path, 'w', encoding='utf-8') as f:",
            '            json.dump(state, f)',
        ])

        # Only emit _parse_value for delimited formats
        if self.file_ext in ('csv', 'tsv'):
            lines.extend([
                '',
                '    def _parse_value(self, value: str) -> Any:',
                "        if value == '':",
                '            return None',
                "        if value.lower() == 'true':",
                '            return True',
                "        if value.lower() == 'false':",
                '            return False',
                '        try:',
                '            return int(value)',
                '        except ValueError:',
                '            pass',
                '        try:',
                '            return float(value)',
                '        except ValueError:',
                '            pass',
                '        return value',
            ])

        # Generate state management methods
        if has_stateful:
            lines.extend(self._generate_state_methods())

        lines.extend([
            '',
            '    def _should_keep_row(self, row: Dict[str, Any]) -> bool:',
        ])
        lines.extend(self._generate_filter_logic())

        # Add neighbor filter logic if needed
        if has_neighbor_filters:
            lines.extend(self._generate_neighbor_filter_methods())

        lines.extend([
            '',
            '    def _transform_row(self, row: Dict[str, Any]) -> Dict[str, Any]:',
        ])

        # Check if we have stateful transforms
        transforms = self.config.get('column_transforms', {})
        output_cols = self.config.get('output_columns', [])

        lines.append('        result = {}')
        lines.extend(self._generate_transform_logic())

        # Add stateful transform application
        if has_stateful:
            lines.extend(self._generate_stateful_transform_logic())

        lines.append('        return result')

        lines.extend([
            '',
            '    def _parse_file(self, path: str) -> Iterator[Dict[str, Any]]:',
        ])

        if self.file_ext in ('csv', 'tsv'):
            lines.extend([
                "        with open(path, 'r', encoding='utf-8') as f:",
                '            lines = f.readlines()',
                '        if not lines:',
                '            return',
                "        headers = lines[0].strip().split('" + delimiter + "')",
                '        for line in lines[1:]:',
                '            line = line.strip()',
                '            if not line:',
                '                continue',
                "            values = line.split('" + delimiter + "')",
                '            row = {header: self._parse_value(values[i]) if i < len(values) else None',
                '                   for i, header in enumerate(headers)}',
                '            yield row',
            ])
        elif self.file_ext == 'jsonl':
            lines.extend([
                "        with open(path, 'r', encoding='utf-8') as f:",
                '            for line in f:',
                '                line = line.strip()',
                '                if line:',
                '                    yield json.loads(line)',
            ])
        elif self.file_ext == 'json':
            lines.extend([
                "        with open(path, 'r', encoding='utf-8') as f:",
                '            for row in json.load(f):',
                '                yield row',
            ])

        lines.extend(self._generate_main_loop(has_stateful, has_neighbor_filters))
        return '\n'.join(lines)

    def _generate_state_init(self) -> List[str]:
        """Generate state variable initialization."""
        lines = []
        stateful_transforms = self.config.get('stateful_transforms', [])

        for i, st in enumerate(stateful_transforms):
            st_type = st.get('type')
            if st_type == 'prefix_sum':
                lines.append(f'        self._prefix_sum_{i} = 0.0')
            elif st_type == 'prefix_count':
                lines.append(f'        self._prefix_count_{i} = 0')
            elif st_type == 'sliding_window':
                window_size = st.get('window_size', 3)
                lines.append(f'        self._window_{i} = deque(maxlen={window_size})')
            elif st_type == 'state_machine':
                initial_state = st.get('initial_state', 1)
                lines.append(f'        self._state_{i} = {repr(initial_state)}')

        return lines

    def _generate_state_methods(self) -> List[str]:
        """Generate state save/restore methods."""
        stateful_transforms = self.config.get('stateful_transforms', [])

        lines = [
            '',
            '    def _get_current_state(self) -> Dict:',
            '        return {',
        ]

        for i, st in enumerate(stateful_transforms):
            st_type = st.get('type')
            if st_type == 'prefix_sum':
                lines.append(f'            "prefix_sum_{i}": self._prefix_sum_{i},')
            elif st_type == 'prefix_count':
                lines.append(f'            "prefix_count_{i}": self._prefix_count_{i},')
            elif st_type == 'sliding_window':
                lines.append(f'            "window_{i}": list(self._window_{i}),')
            elif st_type == 'state_machine':
                lines.append(f'            "state_{i}": self._state_{i},')

        lines.extend([
            '        }',
            '',
            '    def _restore_state(self, state: Dict):',
        ])

        for i, st in enumerate(stateful_transforms):
            st_type = st.get('type')
            if st_type == 'prefix_sum':
                lines.append(f'        self._prefix_sum_{i} = state.get("prefix_sum_{i}", 0.0)')
            elif st_type == 'prefix_count':
                lines.append(f'        self._prefix_count_{i} = state.get("prefix_count_{i}", 0)')
            elif st_type == 'sliding_window':
                lines.append(f'        self._window_{i} = deque(state.get("window_{i}", []), maxlen={st.get("window_size", 3)})')
            elif st_type == 'state_machine':
                initial = st.get('initial_state', 1)
                lines.append(f'        self._state_{i} = state.get("state_{i}", {repr(initial)})')

        return lines

    def _generate_filter_logic(self) -> List[str]:
        conditions = self.config.get('filter_conditions', [])
        if not conditions:
            return ['        return True']

        lines = []
        for cond in conditions:
            col = cond['column']
            op = cond['operator']
            val = cond['value']
            val_str = self._val_str(val)
            if op == '!=':
                if isinstance(val, bool):
                    lines.append("        if row.get('" + col + "') is " + val_str + ":")
                else:
                    lines.append("        if row.get('" + col + "') == " + val_str + ":")
                lines.append("            return False")
            elif op == '==':
                lines.append("        if row.get('" + col + "') != " + val_str + ":")
                lines.append("            return False")
            elif op in ('>', '>=', '<', '<='):
                comp_ops = {'>': '<=', '>=': '<', '<': '>=', '<=': '>'}
                lines.append("        val = row.get('" + col + "')")
                lines.append("        if val is not None and isinstance(val, (int, float)) and val " + comp_ops[op] + " " + val_str + ":")
                lines.append("            return False")
        lines.append("        return True")
        return lines

    def _generate_neighbor_filter_methods(self) -> List[str]:
        """Generate methods for neighbor-based filtering."""
        neighbor_filters = self.config.get('neighbor_filters', [])
        lines = []

        for i, nf in enumerate(neighbor_filters):
            nf_type = nf.get('type')

            if nf_type == 'next_row_condition':
                col = nf.get('column')
                val = nf.get('value')
                val_str = self._val_str(val)
                lines.extend([
                    '',
                    '    def _check_neighbor_filter_' + str(i) + '(self, current_row: Dict, next_row: Optional[Dict]) -> bool:',
                    '        """Check if current row should be dropped based on next row."""',
                    '        if next_row is None:',
                    '            return True  # Keep if no next row',
                    "        if next_row.get('" + col + "') == " + val_str + ":",
                    '            return False  # Drop if next row has the condition',
                    '        return True',
                ])
            elif nf_type == 'consecutive_duplicate':
                col = nf.get('column')
                lines.extend([
                    '',
                    '    def _check_neighbor_filter_' + str(i) + '(self, current_row: Dict, prev_row: Optional[Dict]) -> bool:',
                    '        """Check if current row should be dropped as consecutive duplicate."""',
                    '        if prev_row is None:',
                    '            return True  # Keep first row',
                    "        if prev_row.get('" + col + "') == current_row.get('" + col + "'):",
                    '            return False  # Drop if same as previous',
                    '        return True',
                ])

        return lines

    def _val_str(self, val: Any) -> str:
        return _val_str(val, 'py')

    def _generate_transform_logic(self) -> List[str]:
        transforms = self.config.get('column_transforms', {})
        stateful_transforms = self.config.get('stateful_transforms', [])
        stateful_output_cols = {st.get('output_column') for st in stateful_transforms}
        output_cols = self.config.get('output_columns', [])
        lines = []

        for out_col in output_cols:
            if out_col in stateful_output_cols:
                continue  # Will be handled separately

            transform = transforms.get(out_col, {'type': 'unknown'})
            t_type = transform.get('type')
            source = transform.get('source', out_col)
            if t_type in ('identity', 'copy'):
                lines.append("        result['" + out_col + "'] = row.get('" + source + "')")
            elif t_type == 'constant':
                lines.append("        result['" + out_col + "'] = " + self._val_str(transform['value']))
            elif t_type in ('strip', 'lower', 'upper', 'strip_lower', 'strip_upper'):
                method_map = {'strip': 'strip', 'lower': 'lower', 'upper': 'upper',
                             'strip_lower': 'strip().lower', 'strip_upper': 'strip().upper'}
                lines.append("        val = row.get('" + source + "')")
                lines.append("        result['" + out_col + "'] = val." + method_map[t_type] + "() if isinstance(val, str) else val")
            elif t_type == 'add_prefix':
                lines.append("        val = row.get('" + source + "')")
                lines.append("        result['" + out_col + "'] = '" + transform['prefix'] + "' + str(val) if val is not None else None")
            elif t_type == 'add_suffix':
                lines.append("        val = row.get('" + source + "')")
                lines.append("        result['" + out_col + "'] = str(val) + '" + transform['suffix'] + "' if val is not None else None")
            elif t_type == 'linear':
                a, b = transform['a'], transform['b']
                lines.append("        val = row.get('" + source + "')")
                lines.append("        result['" + out_col + "'] = " + str(a) + " * val + " + str(b) + " if isinstance(val, (int, float)) else None")
            elif t_type != 'stateful':
                lines.append("        result['" + out_col + "'] = row.get('" + out_col + "')")

        return lines

    def _generate_stateful_transform_logic(self) -> List[str]:
        """Generate code for applying stateful transforms."""
        stateful_transforms = self.config.get('stateful_transforms', [])
        lines = []

        for i, st in enumerate(stateful_transforms):
            st_type = st.get('type')
            out_col = st.get('output_column')
            source = st.get('source')
            a = st.get('a', 1.0)
            b = st.get('b', 0.0)

            if st_type == 'prefix_sum':
                lines.extend([
                    "        val = row.get('" + source + "')",
                    '        if isinstance(val, (int, float)):',
                    f'            self._prefix_sum_{i} += val',
                    f'        result["{out_col}"] = {a} * self._prefix_sum_{i} + {b}',
                ])
            elif st_type == 'prefix_count':
                condition = st.get('condition')
                if condition == 'not_null':
                    lines.extend([
                        "        val = row.get('" + str(source) + "')",
                        '        if val is not None:',
                        f'            self._prefix_count_{i} += 1',
                    ])
                elif condition == 'positive':
                    lines.extend([
                        "        val = row.get('" + str(source) + "')",
                        '        if isinstance(val, (int, float)) and val > 0:',
                        f'            self._prefix_count_{i} += 1',
                    ])
                elif condition == 'negative':
                    lines.extend([
                        "        val = row.get('" + str(source) + "')",
                        '        if isinstance(val, (int, float)) and val < 0:',
                        f'            self._prefix_count_{i} += 1',
                    ])
                elif condition == 'true':
                    lines.extend([
                        "        val = row.get('" + str(source) + "')",
                        '        if val is True:',
                        f'            self._prefix_count_{i} += 1',
                    ])
                else:
                    # Default: increment for every row
                    lines.extend([
                        f'        self._prefix_count_{i} += 1',
                    ])
                lines.append(f'        result["{out_col}"] = {a} * self._prefix_count_{i} + {b}')

            elif st_type == 'sliding_window':
                window_size = st.get('window_size', 3)
                operation = st.get('operation', 'mean')

                lines.extend([
                    "        val = row.get('" + source + "')",
                    '        if isinstance(val, (int, float)):',
                    f'            self._window_{i}.append(val)',
                ])

                if operation == 'sum':
                    lines.append(f'        window_val = sum(self._window_{i})')
                elif operation == 'mean':
                    lines.append(f'        window_val = sum(self._window_{i}) / len(self._window_{i}) if self._window_{i} else 0.0')

                lines.append(f'        result["{out_col}"] = {a} * window_val + {b}')

            elif st_type == 'state_machine':
                transitions = st.get('transitions', [])
                initial_state = st.get('initial_state')

                # Get value once before checking transitions
                lines.extend([
                    "        val = row.get('" + source + "')",
                    '        if isinstance(val, (int, float)):',
                ])

                for t in transitions:
                    t_cond = t.get('condition')
                    if t_cond == 'threshold_cross':
                        threshold = t.get('threshold')
                        direction = t.get('direction')
                        target_state = t.get('target_state')

                        if target_state is not None:
                            if direction == 'up':
                                lines.extend([
                                    f'            if val >= {threshold} and self._state_{i} < {target_state}:',
                                    f'                self._state_{i} = {target_state}',
                                ])
                            else:
                                lines.extend([
                                    f'            if val < {threshold} and self._state_{i} < {target_state}:',
                                    f'                self._state_{i} = {target_state}',
                                ])

                lines.append(f'        result["{out_col}"] = self._state_{i}')

        return lines

    def _generate_main_loop(self, has_stateful: bool, has_neighbor_filters: bool) -> List[str]:
        """Generate the main processing loop."""
        lines = [
            '',
            '    def __call__(self, path: str) -> Iterator[Dict[str, Any]]:',
            '        cache_path = self._get_cache_path(path)',
            '        cache_state = self._load_cache(cache_path)',
            '        start_row = cache_state.get("processed_rows", 0)',
            '        row_count = 0',
            '        buffer_count = 0',
            '        current_cache_rows = []',
        ]

        if has_neighbor_filters:
            lines.extend([
                '        pending_row = None',
                '        pending_input_idx = None',
                '        prev_row = None',
            ])

        lines.extend([
            '',
            '        for row in self._parse_file(path):',
        ])

        if has_neighbor_filters:
            neighbor_filters = self.config.get('neighbor_filters', [])
            lines.extend(self._generate_neighbor_filter_logic(neighbor_filters))
            # When neighbor filters are active, rows are processed through the pending mechanism
            # No additional transform/yield code needed here
        else:
            lines.extend([
                '            if row_count < start_row:',
                '                row_count += 1',
                '                continue',
                '            if not self._should_keep_row(row):',
                '                row_count += 1',
                '                cache_state["processed_rows"] = row_count',
                '                self._save_cache(cache_path, cache_state)',
                '                continue',
                '',
                '            transformed = self._transform_row(row)',
                '            if cache_path is not None:',
                '                current_cache_rows.append(transformed)',
                '                buffer_count += 1',
                '                if buffer_count >= self.buffer:',
                '                    cache_state["processed_rows"] = row_count + 1',
                '                    cache_state["rows"] = current_cache_rows',
                '                    self._save_cache(cache_path, cache_state)',
                '                    current_cache_rows = []',
                '                    buffer_count = 0',
                '',
                '            row_count += 1',
                '            yield transformed',
            ])

        lines.append('')

        # Handle pending row at end for neighbor filters
        if has_neighbor_filters:
            lines.extend([
                '        # Handle any pending row at end of file',
                '        if pending_row is not None:',
                '            # Check against all neighbor filters with no next row',
                '            keep = True',
            ])

            for i, nf in enumerate(neighbor_filters):
                nf_type = nf.get('type')
                if nf_type == 'next_row_condition':
                    lines.append(f'            keep = keep and self._check_neighbor_filter_{i}(pending_row, None)')

            lines.extend([
                '            if keep and self._should_keep_row(pending_row):',
                '                transformed = self._transform_row(pending_row)',
                '                if cache_path is not None:',
                '                    current_cache_rows.append(transformed)',
                '                    cache_state["processed_rows"] = row_count + 1',
                '                    cache_state["rows"] = current_cache_rows',
                '                    self._save_cache(cache_path, cache_state)',
                '                yield transformed',
                '',
            ])

        lines.extend([
            '        if cache_path is not None and (buffer_count > 0 or row_count > start_row):',
            '            cache_state["processed_rows"] = row_count',
            '            cache_state["rows"] = current_cache_rows',
            '            self._save_cache(cache_path, cache_state)',
            '',
        ])

        return lines

    def _generate_neighbor_filter_logic(self, neighbor_filters: List[Dict]) -> List[str]:
        """Generate the logic for handling neighbor-based filtering."""
        lines = [
            '            if row_count < start_row:',
            '                if pending_row is not None and pending_input_idx == row_count:',
            '                    # This row was pending, now process it with current as next',
            '                    keep = True',
        ]

        # Check all neighbor filters
        for i, nf in enumerate(neighbor_filters):
            nf_type = nf.get('type')
            if nf_type == 'next_row_condition':
                lines.append(f'                    keep = keep and self._check_neighbor_filter_{i}(pending_row, row)')
            elif nf_type == 'consecutive_duplicate':
                lines.append(f'                    keep = keep and self._check_neighbor_filter_{i}(pending_row, prev_row)')

        lines.extend([
            '                    if keep and self._should_keep_row(pending_row):',
            '                        transformed = self._transform_row(pending_row)',
            '                        if cache_path is not None:',
            '                            current_cache_rows.append(transformed)',
            '                            buffer_count += 1',
            '                            if buffer_count >= self.buffer:',
            '                                cache_state["processed_rows"] = row_count',
            '                                cache_state["rows"] = current_cache_rows',
            '                                self._save_cache(cache_path, cache_state)',
            '                                current_cache_rows = []',
            '                                buffer_count = 0',
            '                        yield transformed',
            '                    pending_row = None',
            '                    pending_input_idx = None',
            '                prev_row = row',
            '                row_count += 1',
            '                continue',
            '',
            '            # Check if this is the continuation of a pending row',
            '            if pending_row is not None:',
            '                keep = True',
        ])

        for i, nf in enumerate(neighbor_filters):
            nf_type = nf.get('type')
            if nf_type == 'next_row_condition':
                lines.append(f'                keep = keep and self._check_neighbor_filter_{i}(pending_row, row)')
            elif nf_type == 'consecutive_duplicate':
                lines.append(f'                keep = keep and self._check_neighbor_filter_{i}(pending_row, prev_row)')

        lines.extend([
            '                if keep and self._should_keep_row(pending_row):',
            '                    transformed = self._transform_row(pending_row)',
            '                    if cache_path is not None:',
            '                        current_cache_rows.append(transformed)',
            '                        buffer_count += 1',
            '                        if buffer_count >= self.buffer:',
            '                            cache_state["processed_rows"] = row_count',
            '                            cache_state["rows"] = current_cache_rows',
            '                            self._save_cache(cache_path, cache_state)',
            '                            current_cache_rows = []',
            '                            buffer_count = 0',
            '                    yield transformed',
            '                pending_row = None',
            '',
            '            # Set this row as pending (need to see next row)',
            '            pending_row = row',
            '            pending_input_idx = row_count',
            '            prev_row = row',
            '            row_count += 1',
        ])

        return lines


# =============================================================================
# JavaScript Code Generator
# =============================================================================

class JavaScriptCodeGenerator:
    """Generates JavaScript module code from transformation config."""

    def __init__(self, module_name: str, config: Dict, file_ext: str):
        self.module_name = module_name
        self.config = config
        self.file_ext = file_ext

    def generate(self) -> str:
        delimiter = ',' if self.file_ext == 'csv' else '\t' if self.file_ext == 'tsv' else None
        has_stateful = bool(self.config.get('stateful_transforms'))
        has_neighbor_filters = bool(self.config.get('neighbor_filters'))

        lines = [
            '/**',
            ' * Dynamic Preprocessor - Streaming data processor with caching support.',
            ' * Supports stateful transforms and neighbor-based filtering.',
            ' */',
            '',
            "const fs = require('fs');",
            "const path = require('path');",
            "const crypto = require('crypto');",
            '',
            'const CONFIG = ' + json.dumps(self.config, indent=2) + ';',
            '',
        ]

        lines.extend([
            'function parseValue(value) {',
            "    if (value === '') return null;",
            "    if (value.toLowerCase() === 'true') return true;",
            "    if (value.toLowerCase() === 'false') return false;",
            '    const intVal = parseInt(value, 10);',
            '    if (!isNaN(intVal) && String(intVal) === value) return intVal;',
            '    const floatVal = parseFloat(value);',
            '    if (!isNaN(floatVal)) return floatVal;',
            '    return value;',
            '}',
            '',
            'function shouldKeepRow(row) {',
        ])
        lines.extend(self._generate_filter_logic_js())
        lines.extend([
            '    return true;',
            '}',
            '',
            'function transformRow(row, state) {',
            '    const result = {};',
        ])
        lines.extend(self._generate_transform_logic_js())

        if has_stateful:
            lines.extend(self._generate_stateful_transform_js())

        lines.extend([
            '    return result;',
            '}',
            '',
        ])

        if has_stateful:
            lines.extend(self._generate_js_state_methods())

        lines.extend([
            'function* parseFile(filePath) {',
        ])

        if self.file_ext in ('csv', 'tsv'):
            lines.extend([
                "    const content = fs.readFileSync(filePath, 'utf-8');",
                "    const lines = content.trim().split('\\n');",
                '    if (lines.length === 0) return;',
                '',
                "    const headers = lines[0].split('" + delimiter + "');",
                '    for (let i = 1; i < lines.length; i++) {',
                '        const line = lines[i].trim();',
                '        if (!line) continue;',
                "        const values = line.split('" + delimiter + "');",
                '        const row = {};',
                '        for (let j = 0; j < headers.length; j++) {',
                '            row[headers[j]] = j < values.length ? parseValue(values[j]) : null;',
                '        }',
                '        yield row;',
                '    }',
            ])
        elif self.file_ext == 'jsonl':
            lines.extend([
                "    const content = fs.readFileSync(filePath, 'utf-8');",
                "    const lines = content.trim().split('\\n');",
                '    for (const line of lines) {',
                '        if (line.trim()) {',
                '            yield JSON.parse(line);',
                '        }',
                '    }',
            ])
        elif self.file_ext == 'json':
            lines.extend([
                "    const content = fs.readFileSync(filePath, 'utf-8');",
                '    const data = JSON.parse(content);',
                '    for (const row of data) {',
                '        yield row;',
                '    }',
            ])

        lines.extend([
            '}',
            '',
            'function getCachePath(inputPath, cacheDir) {',
            '    if (!cacheDir) return null;',
            "    const hash = crypto.createHash('md5').update(path.resolve(inputPath)).digest('hex');",
            '    if (!fs.existsSync(cacheDir)) {',
            "        fs.mkdirSync(cacheDir, { recursive: true });",
            '    }',
            "    return path.join(cacheDir, `${hash}.json`);",
            '}',
            '',
            'function loadCache(cachePath, state) {',
            '    if (!cachePath || !fs.existsSync(cachePath)) {',
            '        return { processedRows: 0, rows: [] };',
            '    }',
            "    const data = JSON.parse(fs.readFileSync(cachePath, 'utf-8'));",
            '    if (data.state) {',
            '        restoreState(data.state, state);',
            '    }',
            '    return data;',
            '}',
            '',
            'function saveCache(cachePath, state, processorState) {',
            '    if (!cachePath) return;',
            '    state.state = getCurrentState(processorState);',
            '    fs.writeFileSync(cachePath, JSON.stringify(state));',
            '}',
            '',
            'class DynamicPreprocessor {',
            '    constructor({ buffer, cache_dir = null }) {',
            '        this.buffer = buffer;',
            '        this.cacheDir = cache_dir;',
            '        this.state = this._initState();',
            '    }',
            '',
            '    _initState() {',
            '        return {',
        ])

        # Initialize state for stateful transforms
        if has_stateful:
            stateful_transforms = self.config.get('stateful_transforms', [])
            for i, st in enumerate(stateful_transforms):
                st_type = st.get('type')
                if st_type == 'prefix_sum':
                    lines.append(f'            prefixSum_{i}: 0.0,')
                elif st_type == 'prefix_count':
                    lines.append(f'            prefixCount_{i}: 0,')
                elif st_type == 'sliding_window':
                    lines.append(f'            window_{i}: [],')
                    lines.append(f'            windowSize_{i}: {st.get("window_size", 3)},')
                elif st_type == 'state_machine':
                    initial = st.get('initial_state', 1)
                    lines.append(f'            state_{i}: {repr(initial)},')

        lines.extend([
            '        };',
            '    }',
            '',
            '    process(filePath) {',
            '        const self = this;',
            '        return {',
            '            [Symbol.iterator]: function* () {',
            '                const cachePath = getCachePath(filePath, self.cacheDir);',
            '                const cacheState = loadCache(cachePath, self.state);',
            '                const startRow = cacheState.processedRows || 0;',
            '',
            '                let rowCount = 0;',
            '                let bufferCount = 0;',
            '                const currentCacheRows = [];',
            '',
        ])

        if has_neighbor_filters:
            lines.extend([
                '                let pendingRow = null;',
                '                let pendingInputIdx = null;',
                '                let prevRow = null;',
                '',
            ])

        lines.extend([
            '                for (const row of parseFile(filePath)) {',
        ])

        if has_neighbor_filters:
            lines.extend(self._generate_js_neighbor_filter_logic())
            # When neighbor filters are active, rows are processed through the pending mechanism
        else:
            lines.extend([
                '                    if (rowCount < startRow) {',
                '                        rowCount++;',
                '                        continue;',
                '                    }',
                '',
                '                    if (!shouldKeepRow(row)) {',
                '                        rowCount++;',
                '                        cacheState.processedRows = rowCount;',
                '                        saveCache(cachePath, cacheState, self.state);',
                '                        continue;',
                '                    }',
                '',
                '                    const transformed = transformRow(row, self.state);',
                '',
                '                    if (cachePath) {',
                '                        currentCacheRows.push(transformed);',
                '                        bufferCount++;',
                '',
                '                        if (bufferCount >= self.buffer) {',
                '                            cacheState.processedRows = rowCount + 1;',
                '                            cacheState.rows = currentCacheRows;',
                '                            saveCache(cachePath, cacheState, self.state);',
                '                            currentCacheRows.length = 0;',
                '                            bufferCount = 0;',
                '                        }',
                '                    }',
                '',
                '                    rowCount++;',
                '                    yield transformed;',
            ])

        lines.extend([
            '                }',
            '',
        ])

        # Handle pending row at end
        if has_neighbor_filters:
            lines.extend([
                '                // Handle pending row at end',
                '                if (pendingRow !== null) {',
                '                    let keep = true;',
            ])

            neighbor_filters = self.config.get('neighbor_filters', [])
            for i, nf in enumerate(neighbor_filters):
                nf_type = nf.get('type')
                if nf_type == 'next_row_condition':
                    col = nf.get('column')
                    val = self._val_str(nf.get('value'))
                    lines.extend([
                        f'                    keep = keep && (null === null || pendingRow.next_{col} !== {val});',
                    ])

            lines.extend([
                '                    if (keep && shouldKeepRow(pendingRow)) {',
                '                        const transformed = transformRow(pendingRow, self.state);',
                '                        if (cachePath) {',
                '                            currentCacheRows.push(transformed);',
                '                            cacheState.processedRows = rowCount + 1;',
                '                            cacheState.rows = currentCacheRows;',
                '                            saveCache(cachePath, cacheState, self.state);',
                '                        }',
                '                        yield transformed;',
                '                    }',
                '                }',
                '',
            ])

        lines.extend([
            '                if (cachePath && (bufferCount > 0 || rowCount > startRow)) {',
            '                    cacheState.processedRows = rowCount;',
            '                    cacheState.rows = currentCacheRows;',
            '                    saveCache(cachePath, cacheState, self.state);',
            '                }',
            '            }',
            '        };',
            '    }',
            '',
            '    [Symbol.iterator]() {',
            "        throw new Error('Call process(filePath) to get an iterator');",
            '    }',
            '}',
            '',
            'function createPreprocessor(options) {',
            '    const preprocessor = new DynamicPreprocessor(options);',
            '    return function(filePath) {',
            '        return preprocessor.process(filePath);',
            '    };',
            '}',
            '',
            'module.exports = { DynamicPreprocessor, createPreprocessor };',
            '',
        ])
        return '\n'.join(lines)

    def _generate_js_state_methods(self) -> List[str]:
        """Generate JavaScript state save/restore methods."""
        stateful_transforms = self.config.get('stateful_transforms', [])

        lines = [
            'function getCurrentState(state) {',
            '    return {',
        ]

        for i, st in enumerate(stateful_transforms):
            st_type = st.get('type')
            if st_type == 'prefix_sum':
                lines.append(f'        prefixSum_{i}: state.prefixSum_{i},')
            elif st_type == 'prefix_count':
                lines.append(f'        prefixCount_{i}: state.prefixCount_{i},')
            elif st_type == 'sliding_window':
                lines.append(f'        window_{i}: state.window_{i}.slice(),')
            elif st_type == 'state_machine':
                lines.append(f'        state_{i}: state.state_{i},')

        lines.extend([
            '    };',
            '}',
            '',
            'function restoreState(savedState, state) {',
        ])

        for i, st in enumerate(stateful_transforms):
            st_type = st.get('type')
            if st_type == 'prefix_sum':
                lines.append(f'    state.prefixSum_{i} = savedState.prefixSum_{i} || 0.0;')
            elif st_type == 'prefix_count':
                lines.append(f'    state.prefixCount_{i} = savedState.prefixCount_{i} || 0;')
            elif st_type == 'sliding_window':
                lines.append(f'    state.window_{i} = savedState.window_{i} || [];')
            elif st_type == 'state_machine':
                initial = st.get('initial_state', 1)
                lines.append(f'    state.state_{i} = savedState.state_{i} !== undefined ? savedState.state_{i} : {initial};')

        lines.extend([
            '}',
            '',
        ])

        return lines

    def _val_str(self, val: Any) -> str:
        return _val_str(val, 'js')

    def _generate_filter_logic_js(self) -> List[str]:
        conditions = self.config.get('filter_conditions', [])
        lines = []
        for cond in conditions:
            col = cond['column']
            op = cond['operator']
            val_str = self._val_str(cond['value'])
            if op == '!=':
                lines.append("    if (row['" + col + "'] === " + val_str + ") return false;")
            elif op == '==':
                lines.append("    if (row['" + col + "'] !== " + val_str + ") return false;")
            elif op in ('>', '>=', '<', '<='):
                comp_ops = {'>': '<=', '>=': '<', '<': '>=', '<=': '>'}
                lines.append("    if (typeof row['" + col + "'] === 'number' && row['" + col + "'] " + comp_ops[op] + " " + val_str + ") return false;")
        return lines

    def _generate_transform_logic_js(self) -> List[str]:
        transforms = self.config.get('column_transforms', {})
        stateful_transforms = self.config.get('stateful_transforms', [])
        stateful_output_cols = {st.get('output_column') for st in stateful_transforms}
        output_cols = self.config.get('output_columns', [])
        lines = []
        for out_col in output_cols:
            if out_col in stateful_output_cols:
                continue

            transform = transforms.get(out_col, {'type': 'unknown'})
            t_type = transform.get('type')
            source = transform.get('source', out_col)
            v = "val_" + out_col.replace('-', '_')
            if t_type in ('identity', 'copy'):
                lines.append("    result['" + out_col + "'] = row['" + source + "'];")
            elif t_type == 'constant':
                lines.append("    result['" + out_col + "'] = " + self._val_str(transform['value']) + ";")
            elif t_type in ('strip', 'lower', 'upper', 'strip_lower', 'strip_upper'):
                method_map = {'strip': 'trim', 'lower': 'toLowerCase', 'upper': 'toUpperCase',
                             'strip_lower': 'trim().toLowerCase', 'strip_upper': 'trim().toUpperCase'}
                lines.append("    const " + v + " = row['" + source + "'];")
                lines.append("    result['" + out_col + "'] = typeof " + v + " === 'string' ? " + v + "." + method_map[t_type] + "() : " + v + ";")
            elif t_type == 'add_prefix':
                lines.append("    const " + v + " = row['" + source + "'];")
                lines.append("    result['" + out_col + "'] = " + v + " != null ? '" + transform['prefix'] + "' + String(" + v + ") : null;")
            elif t_type == 'add_suffix':
                lines.append("    const " + v + " = row['" + source + "'];")
                lines.append("    result['" + out_col + "'] = " + v + " != null ? String(" + v + ") + '" + transform['suffix'] + "' : null;")
            elif t_type == 'linear':
                a, b = transform['a'], transform['b']
                lines.append("    const " + v + " = row['" + source + "'];")
                lines.append("    result['" + out_col + "'] = typeof " + v + " === 'number' ? " + str(a) + " * " + v + " + " + str(b) + " : null;")
            elif t_type != 'stateful':
                lines.append("    result['" + out_col + "'] = row['" + out_col + "'];")
        return lines

    def _generate_stateful_transform_js(self) -> List[str]:
        """Generate JavaScript code for stateful transforms."""
        stateful_transforms = self.config.get('stateful_transforms', [])
        lines = []

        for i, st in enumerate(stateful_transforms):
            st_type = st.get('type')
            out_col = st.get('output_column')
            source = st.get('source')
            a = st.get('a', 1.0)
            b = st.get('b', 0.0)

            if st_type == 'prefix_sum':
                lines.extend([
                    "    const val_psum_" + str(i) + " = row['" + str(source) + "'];",
                    '    if (typeof val_psum_' + str(i) + ' === "number") {',
                    '        state.prefixSum_' + str(i) + ' += val_psum_' + str(i) + ';',
                    '    }',
                    '    result["' + out_col + '"] = ' + str(a) + ' * state.prefixSum_' + str(i) + ' + ' + str(b) + ';',
                ])
            elif st_type == 'prefix_count':
                condition = st.get('condition')
                lines.append("    const val_pcnt_" + str(i) + " = row['" + str(source) + "'];")

                if condition == 'not_null':
                    lines.extend([
                        '    if (val_pcnt_' + str(i) + ' !== null && val_pcnt_' + str(i) + ' !== undefined) {',
                        '        state.prefixCount_' + str(i) + '++;',
                        '    }',
                    ])
                elif condition == 'positive':
                    lines.extend([
                        '    if (typeof val_pcnt_' + str(i) + ' === "number" && val_pcnt_' + str(i) + ' > 0) {',
                        '        state.prefixCount_' + str(i) + '++;',
                        '    }',
                    ])
                elif condition == 'negative':
                    lines.extend([
                        '    if (typeof val_pcnt_' + str(i) + ' === "number" && val_pcnt_' + str(i) + ' < 0) {',
                        '        state.prefixCount_' + str(i) + '++;',
                        '    }',
                    ])
                elif condition == 'true':
                    lines.extend([
                        '    if (val_pcnt_' + str(i) + ' === true) {',
                        '        state.prefixCount_' + str(i) + '++;',
                        '    }',
                    ])
                else:
                    lines.append('    state.prefixCount_' + str(i) + '++;')

                lines.append('    result["' + out_col + '"] = ' + str(a) + ' * state.prefixCount_' + str(i) + ' + ' + str(b) + ';')

            elif st_type == 'sliding_window':
                window_size = st.get('window_size', 3)
                operation = st.get('operation', 'mean')

                lines.extend([
                    "    const val_win_" + str(i) + " = row['" + str(source) + "'];",
                    '    if (typeof val_win_' + str(i) + ' === "number") {',
                    '        state.window_' + str(i) + '.push(val_win_' + str(i) + ');',
                    '        if (state.window_' + str(i) + '.length > state.windowSize_' + str(i) + ') {',
                    '            state.window_' + str(i) + '.shift();',
                    '        }',
                    '    }',
                ])

                if operation == 'sum':
                    lines.append('    const windowVal_' + str(i) + ' = state.window_' + str(i) + '.reduce((a, b) => a + b, 0);')
                elif operation == 'mean':
                    lines.extend([
                        '    const windowSum_' + str(i) + ' = state.window_' + str(i) + '.reduce((a, b) => a + b, 0);',
                        '    const windowVal_' + str(i) + ' = state.window_' + str(i) + '.length > 0 ? windowSum_' + str(i) + ' / state.window_' + str(i) + '.length : 0.0;',
                    ])

                lines.append('    result["' + out_col + '"] = ' + str(a) + ' * windowVal_' + str(i) + ' + ' + str(b) + ';')

            elif st_type == 'state_machine':
                transitions = st.get('transitions', [])

                # Declare the value variable once before the loop
                lines.extend([
                    "    const val_sm_" + str(i) + " = row['" + str(source) + "'];",
                    '    if (typeof val_sm_' + str(i) + ' === "number") {',
                ])

                for t in transitions:
                    t_cond = t.get('condition')
                    if t_cond == 'threshold_cross':
                        threshold = t.get('threshold')
                        direction = t.get('direction')
                        target_state = t.get('target_state')

                        if target_state is not None:
                            if direction == 'up':
                                lines.extend([
                                    '        if (val_sm_' + str(i) + ' >= ' + str(threshold) + ' && state.state_' + str(i) + ' < ' + str(target_state) + ') {',
                                    '            state.state_' + str(i) + ' = ' + str(target_state) + ';',
                                    '        }',
                                ])
                            else:
                                lines.extend([
                                    '        if (val_sm_' + str(i) + ' < ' + str(threshold) + ' && state.state_' + str(i) + ' < ' + str(target_state) + ') {',
                                    '            state.state_' + str(i) + ' = ' + str(target_state) + ';',
                                    '        }',
                                ])

                lines.append('    }')
                lines.append('    result["' + out_col + '"] = state.state_' + str(i) + ';')

        return lines

    def _generate_js_neighbor_filter_logic(self) -> List[str]:
        """Generate JavaScript logic for neighbor-based filtering."""
        neighbor_filters = self.config.get('neighbor_filters', [])

        lines = [
            '                    if (rowCount < startRow) {',
            '                        if (pendingRow !== null && pendingInputIdx === rowCount) {',
            '                            let keep = true;',
        ]

        for i, nf in enumerate(neighbor_filters):
            nf_type = nf.get('type')
            if nf_type == 'next_row_condition':
                col = nf.get('column')
                val = self._val_str(nf.get('value'))
                lines.append(f'                            keep = keep && (row["{col}"] !== {val});')
            elif nf_type == 'consecutive_duplicate':
                col = nf.get('column')
                lines.append(f'                            keep = keep && (prevRow === null || prevRow["{col}"] !== pendingRow["{col}"]);')

        lines.extend([
            '                            if (keep && shouldKeepRow(pendingRow)) {',
            '                                const transformed = transformRow(pendingRow, self.state);',
            '                                if (cachePath) {',
            '                                    currentCacheRows.push(transformed);',
            '                                    bufferCount++;',
            '                                    if (bufferCount >= self.buffer) {',
            '                                        cacheState.processedRows = rowCount;',
            '                                        cacheState.rows = currentCacheRows;',
            '                                        saveCache(cachePath, cacheState, self.state);',
            '                                        currentCacheRows.length = 0;',
            '                                        bufferCount = 0;',
            '                                    }',
            '                                }',
            '                                yield transformed;',
            '                            }',
            '                            pendingRow = null;',
            '                            pendingInputIdx = null;',
            '                        }',
            '                        prevRow = row;',
            '                        rowCount++;',
            '                        continue;',
            '                    }',
            '',
            '                    if (pendingRow !== null) {',
            '                        let keep = true;',
        ])

        for i, nf in enumerate(neighbor_filters):
            nf_type = nf.get('type')
            if nf_type == 'next_row_condition':
                col = nf.get('column')
                val = self._val_str(nf.get('value'))
                lines.append(f'                        keep = keep && (row["{col}"] !== {val});')
            elif nf_type == 'consecutive_duplicate':
                col = nf.get('column')
                lines.append(f'                        keep = keep && (prevRow === null || prevRow["{col}"] !== pendingRow["{col}"]);')

        lines.extend([
            '                        if (keep && shouldKeepRow(pendingRow)) {',
            '                            const transformed = transformRow(pendingRow, self.state);',
            '                            if (cachePath) {',
            '                                currentCacheRows.push(transformed);',
            '                                bufferCount++;',
            '                                if (bufferCount >= self.buffer) {',
            '                                    cacheState.processedRows = rowCount;',
            '                                    cacheState.rows = currentCacheRows;',
            '                                    saveCache(cachePath, cacheState, self.state);',
            '                                    currentCacheRows.length = 0;',
            '                                    bufferCount = 0;',
            '                                }',
            '                            }',
            '                            yield transformed;',
            '                        }',
            '                        pendingRow = null;',
            '                    }',
            '',
            '                    pendingRow = row;',
            '                    pendingInputIdx = rowCount;',
            '                    prevRow = row;',
            '                    rowCount++;',
        ])

        return lines


# =============================================================================
# C++ Code Generator
# =============================================================================

class CppCodeGenerator:
    """Generates C++ module code from transformation config."""

    def __init__(self, module_name: str, config: Dict, file_ext: str):
        self.module_name = module_name
        self.config = config
        self.file_ext = file_ext

    def generate(self) -> Dict[str, str]:
        return {
            'dynamic_preprocessor.h': self._generate_header(),
            'dynamic_preprocessor.cpp': self._generate_implementation(),
        }

    def _generate_header(self) -> str:
        lines = [
            '#pragma once',
            '',
            '#include <map>',
            '#include <string>',
            '#include <optional>',
            '#include <cstddef>',
            '',
            f'namespace {self.module_name} {{',
            '',
            'enum class ValueType {',
            '    Null,',
            '    Bool,',
            '    Int,',
            '    Double,',
            '    String',
            '};',
            '',
            'struct Value {',
            '    ValueType type = ValueType::Null;',
            '    bool bool_value = false;',
            '    long long int_value = 0;',
            '    double double_value = 0.0;',
            '    std::string string_value;',
            '',
            '    Value() : type(ValueType::Null) {}',
            '    explicit Value(bool v) : type(ValueType::Bool), bool_value(v) {}',
            '    explicit Value(long long v) : type(ValueType::Int), int_value(v) {}',
            '    explicit Value(int v) : type(ValueType::Int), int_value(v) {}',
            '    explicit Value(double v) : type(ValueType::Double), double_value(v) {}',
            '    explicit Value(const std::string& v) : type(ValueType::String), string_value(v) {}',
            '    explicit Value(const char* v) : type(ValueType::String), string_value(v) {}',
            '};',
            '',
            'using Row = std::map<std::string, Value>;',
            '',
            'class DynamicPreprocessor {',
            'public:',
            '    explicit DynamicPreprocessor(std::size_t buffer);',
            '    DynamicPreprocessor(std::size_t buffer, const std::string& cache_dir);',
            '    ~DynamicPreprocessor();',
            '',
            '    void open(const std::string& path);',
            '    bool next(Row& out);',
            '',
            'private:',
            '    struct Impl;',
            '    Impl* impl_;',
            '};',
            '',
            f'}} // namespace {self.module_name}',
            '',
        ]
        return '\n'.join(lines)

    def _generate_implementation(self) -> str:
        has_stateful = bool(self.config.get('stateful_transforms'))
        has_neighbor_filters = bool(self.config.get('neighbor_filters'))
        delimiter = ',' if self.file_ext == 'csv' else '\t' if self.file_ext == 'tsv' else None

        lines = [
            f'#include "dynamic_preprocessor.h"',
            '',
            '#include <fstream>',
            '#include <sstream>',
            '#include <vector>',
            '#include <deque>',
            '#include <filesystem>',
            '#include <functional>',
            '#include <algorithm>',
            '#include <cmath>',
            '#include <cstdint>',
            '',
            f'namespace {self.module_name} {{',
            '',
            '// Helper to parse string values',
            'Value parseValue(const std::string& s) {',
            '    if (s.empty()) return Value();',
            '    if (s == "true") return Value(true);',
            '    if (s == "false") return Value(false);',
            '    // Try integer',
            '    try {',
            '        size_t pos;',
            '        long long val = std::stoll(s, &pos);',
            '        if (pos == s.size()) return Value(val);',
            '    } catch (...) {}',
            '    // Try double',
            '    try {',
            '        size_t pos;',
            '        double val = std::stod(s, &pos);',
            '        if (pos == s.size()) return Value(val);',
            '    } catch (...) {}',
            '    return Value(s);',
            '}',
            '',
            '// MD5 implementation for cache key',
            'std::string md5(const std::string& input) {',
            '    uint8_t digest[16];',
            '    // Simple hash for cross-platform compatibility',
            '    uint32_t hash = 5381;',
            '    for (char c : input) hash = ((hash << 5) + hash) + c;',
            '    for (char c : input) hash = ((hash << 5) + hash) + c;',
            '    char buf[33];',
            '    snprintf(buf, sizeof(buf), "%08x%08x%08x%08x", hash, hash ^ 0xDEADBEEF, hash ^ 0xCAFEBABE, hash ^ 0x12345678);',
            '    return std::string(buf, 32);',
            '}',
            '',
            '// JSON-like serialization for Value',
            'std::string valueToJson(const Value& v) {',
            '    switch (v.type) {',
            '        case ValueType::Null: return "null";',
            '        case ValueType::Bool: return v.bool_value ? "true" : "false";',
            '        case ValueType::Int: return std::to_string(v.int_value);',
            '        case ValueType::Double: {',
            '            std::ostringstream oss;',
            '            oss << v.double_value;',
            '            return oss.str();',
            '        }',
            '        case ValueType::String: {',
            '            std::string result = "\\"";',
            '            for (char c : v.string_value) {',
            '                if (c == \'\\\\\' || c == \'\\"\') result += \'\\\\\';',
            '                result += c;',
            '            }',
            '            result += \'\"\';',
            '            return result;',
            '        }',
            '    }',
            '    return "null";',
            '}',
            '',
            'Value jsonToValue(const std::string& s) {',
            '    if (s == "null") return Value();',
            '    if (s == "true") return Value(true);',
            '    if (s == "false") return Value(false);',
            '    if (s.size() >= 2 && s.front() == \'\\"\' && s.back() == \'\\"\') {',
            '        std::string inner = s.substr(1, s.size() - 2);',
            '        std::string result;',
            '        for (size_t i = 0; i < inner.size(); ++i) {',
            '            if (inner[i] == \'\\\\\' && i + 1 < inner.size()) {',
            '                result += inner[++i];',
            '            } else {',
            '                result += inner[i];',
            '            }',
            '        }',
            '        return Value(result);',
            '    }',
            '    try { return Value(std::stoll(s)); } catch (...) {}',
            '    try { return Value(std::stod(s)); } catch (...) {}',
            '    return Value(s);',
            '}',
            '',
            'struct DynamicPreprocessor::Impl {',
            '    std::size_t buffer;',
            '    std::string cacheDir;',
            '    std::string currentPath;',
            '    std::vector<std::map<std::string, std::string>> rawRows;',
            '    size_t currentIdx = 0;',
            '    size_t processedRows = 0;',
            '    std::vector<Row> cachedRows;',
            '',
        ]

        # Add state variables for stateful transforms
        if has_stateful:
            lines.extend(self._generate_cpp_state_vars())

        lines.extend([
            '    Impl(std::size_t buf, const std::string& cd) : buffer(buf), cacheDir(cd) {}',
            '',
            '    std::string getCachePath(const std::string& path) {',
            '        if (cacheDir.empty()) return "";',
            '        std::filesystem::create_directories(cacheDir);',
            '        std::string absPath = std::filesystem::absolute(path).string();',
            '        return cacheDir + "/" + md5(absPath) + ".json";',
            '    }',
            '',
            '    void loadCache(const std::string& cachePath) {',
            '        processedRows = 0;',
            '        cachedRows.clear();',
            '        if (cachePath.empty()) return;',
            '        std::ifstream f(cachePath);',
            '        if (!f.is_open()) return;',
            '        std::stringstream ss;',
            '        ss << f.rdbuf();',
            '        std::string content = ss.str();',
            '        // Parse JSON-like cache',
            '        size_t pos = content.find("\\"processed_rows\\"");',
            '        if (pos != std::string::npos) {',
            '            pos = content.find(":", pos);',
            '            if (pos != std::string::npos) {',
            '                processedRows = std::stoull(content.substr(pos + 1));',
            '            }',
            '        }',
        ])

        if has_stateful:
            lines.extend(self._generate_cpp_state_load())

        lines.extend([
            '    }',
            '',
            '    void saveCache(const std::string& cachePath) {',
            '        if (cachePath.empty()) return;',
            '        std::ofstream f(cachePath);',
            '        if (!f.is_open()) return;',
            '        f << "{\\"processed_rows\\": " << processedRows;',
            '        f << ", \\"rows\\": [";',
            '        for (size_t i = 0; i < cachedRows.size(); ++i) {',
            '            if (i > 0) f << ", ";',
            '            f << "{";',
            '            bool first = true;',
            '            for (const auto& [k, v] : cachedRows[i]) {',
            '                if (!first) f << ", ";',
            '                first = false;',
            '                f << "\\"" << k << "\\": " << valueToJson(v);',
            '            }',
            '            f << "}";',
            '        }',
            '        f << "], \\"state\\": {";',
        ])

        if has_stateful:
            lines.extend(self._generate_cpp_state_save())

        lines.extend([
            '        f << "}}";',
            '    }',
            '',
        ])

        # Add filter method
        lines.extend(self._generate_cpp_should_keep_row())

        # Add transform method
        lines.extend(self._generate_cpp_transform_row())

        lines.extend([
            '    void parseFile(const std::string& path) {',
            '        rawRows.clear();',
        ])

        if self.file_ext == 'csv':
            lines.extend([
                '        std::ifstream f(path);',
                '        if (!f.is_open()) return;',
                '        std::string line;',
                '        std::getline(f, line);',
                '        std::vector<std::string> headers;',
                '        std::stringstream hs(line);',
                '        std::string h;',
                '        while (std::getline(hs, h, \',\')) headers.push_back(h);',
                '        while (std::getline(f, line)) {',
                '            if (line.empty()) continue;',
                '            std::map<std::string, std::string> row;',
                '            std::stringstream ls(line);',
                '            std::string val;',
                '            for (size_t i = 0; i < headers.size() && std::getline(ls, val, \',\'); ++i) {',
                '                row[headers[i]] = val;',
                '            }',
                '            rawRows.push_back(row);',
                '        }',
            ])
        elif self.file_ext == 'tsv':
            lines.extend([
                '        std::ifstream f(path);',
                '        if (!f.is_open()) return;',
                '        std::string line;',
                '        std::getline(f, line);',
                '        std::vector<std::string> headers;',
                '        std::stringstream hs(line);',
                '        std::string h;',
                '        while (std::getline(hs, h, \'\\t\')) headers.push_back(h);',
                '        while (std::getline(f, line)) {',
                '            if (line.empty()) continue;',
                '            std::map<std::string, std::string> row;',
                '            std::stringstream ls(line);',
                '            std::string val;',
                '            for (size_t i = 0; i < headers.size() && std::getline(ls, val, \'\\t\'); ++i) {',
                '                row[headers[i]] = val;',
                '            }',
                '            rawRows.push_back(row);',
                '        }',
            ])
        elif self.file_ext == 'jsonl':
            lines.extend([
                '        std::ifstream f(path);',
                '        if (!f.is_open()) return;',
                '        std::string line;',
                '        while (std::getline(f, line)) {',
                '            if (line.empty()) continue;',
                '            // Simple JSONL parsing',
                '            std::map<std::string, std::string> row;',
                '            size_t pos = 0;',
                '            while ((pos = line.find("\\"", pos)) != std::string::npos) {',
                '                size_t keyStart = pos + 1;',
                '                size_t keyEnd = line.find("\\"", keyStart);',
                '                if (keyEnd == std::string::npos) break;',
                '                std::string key = line.substr(keyStart, keyEnd - keyStart);',
                '                size_t colon = line.find(":", keyEnd);',
                '                if (colon == std::string::npos) break;',
                '                size_t valStart = line.find_first_not_of(" \\t", colon + 1);',
                '                if (valStart == std::string::npos) break;',
                '                size_t valEnd;',
                '                if (line[valStart] == \'\\"\') {',
                '                    valEnd = line.find("\\"", valStart + 1);',
                '                    if (valEnd != std::string::npos) valEnd++;',
                '                } else if (line[valStart] == \'{\' || line[valStart] == \'[\') {',
                '                    int depth = 1;',
                '                    valEnd = valStart + 1;',
                '                    while (valEnd < line.size() && depth > 0) {',
                '                        if (line[valEnd] == \'{\' || line[valEnd] == \'[\') depth++;',
                '                        else if (line[valEnd] == \'}\' || line[valEnd] == \']\') depth--;',
                '                        valEnd++;',
                '                    }',
                '                } else {',
                '                    valEnd = line.find_first_of(",}]", valStart);',
                '                    if (valEnd == std::string::npos) valEnd = line.size();',
                '                }',
                '                std::string val = line.substr(valStart, valEnd - valStart);',
                '                // Remove quotes from string values',
                '                if (val.size() >= 2 && val.front() == \'\\"\' && val.back() == \'\\"\') {',
                '                    val = val.substr(1, val.size() - 2);',
                '                }',
                '                row[key] = val;',
                '                pos = valEnd;',
                '            }',
                '            rawRows.push_back(row);',
                '        }',
            ])
        elif self.file_ext == 'json':
            lines.extend([
                '        std::ifstream f(path);',
                '        if (!f.is_open()) return;',
                '        std::stringstream ss;',
                '        ss << f.rdbuf();',
                '        std::string content = ss.str();',
                '        // Simple JSON array parsing',
                '        size_t pos = content.find("[");',
                '        if (pos == std::string::npos) return;',
                '        pos++;',
                '        while (pos < content.size()) {',
                '            pos = content.find("{", pos);',
                '            if (pos == std::string::npos) break;',
                '            size_t objEnd = pos;',
                '            int depth = 1;',
                '            objEnd++;',
                '            while (objEnd < content.size() && depth > 0) {',
                '                if (content[objEnd] == \'{\' || content[objEnd] == \'[\') depth++;',
                '                else if (content[objEnd] == \'}\' || content[objEnd] == \']\') depth--;',
                '                objEnd++;',
                '            }',
                '            std::string obj = content.substr(pos, objEnd - pos);',
                '            std::map<std::string, std::string> row;',
                '            size_t keyPos = 0;',
                '            while ((keyPos = obj.find("\\"", keyPos)) != std::string::npos) {',
                '                size_t keyStart = keyPos + 1;',
                '                size_t keyEnd = obj.find("\\"", keyStart);',
                '                if (keyEnd == std::string::npos) break;',
                '                std::string key = obj.substr(keyStart, keyEnd - keyStart);',
                '                size_t colon = obj.find(":", keyEnd);',
                '                if (colon == std::string::npos) break;',
                '                size_t valStart = obj.find_first_not_of(" \\t", colon + 1);',
                '                if (valStart == std::string::npos) break;',
                '                size_t valEnd;',
                '                if (obj[valStart] == \'\\"\') {',
                '                    valEnd = obj.find("\\"", valStart + 1);',
                '                    if (valEnd != std::string::npos) valEnd++;',
                '                } else if (obj[valStart] == \'{\' || obj[valStart] == \'[\') {',
                '                    int d = 1;',
                '                    valEnd = valStart + 1;',
                '                    while (valEnd < obj.size() && d > 0) {',
                '                        if (obj[valEnd] == \'{\' || obj[valEnd] == \'[\') d++;',
                '                        else if (obj[valEnd] == \'}\' || obj[valEnd] == \']\') d--;',
                '                        valEnd++;',
                '                    }',
                '                } else {',
                '                    valEnd = obj.find_first_of(",}]", valStart);',
                '                    if (valEnd == std::string::npos) valEnd = obj.size();',
                '                }',
                '                std::string val = obj.substr(valStart, valEnd - valStart);',
                '                if (val.size() >= 2 && val.front() == \'\\"\' && val.back() == \'\\"\') {',
                '                    val = val.substr(1, val.size() - 2);',
                '                }',
                '                row[key] = val;',
                '                keyPos = valEnd;',
                '            }',
                '            rawRows.push_back(row);',
                '            pos = objEnd;',
                '        }',
            ])

        lines.extend([
            '    }',
            '};',
            '',
        ])

        # Neighbor filter handling
        if has_neighbor_filters:
            lines.extend(self._generate_cpp_neighbor_filter_methods())

        # Constructor and destructor
        lines.extend([
            'DynamicPreprocessor::DynamicPreprocessor(std::size_t buffer)',
            '    : impl_(new Impl(buffer, "")) {}',
            '',
            'DynamicPreprocessor::DynamicPreprocessor(std::size_t buffer, const std::string& cache_dir)',
            '    : impl_(new Impl(buffer, cache_dir)) {}',
            '',
            'DynamicPreprocessor::~DynamicPreprocessor() { delete impl_; }',
            '',
            'void DynamicPreprocessor::open(const std::string& path) {',
            '    impl_->currentPath = path;',
            '    impl_->parseFile(path);',
            '    impl_->currentIdx = 0;',
            '    std::string cachePath = impl_->getCachePath(path);',
            '    impl_->loadCache(cachePath);',
        ])

        if has_stateful:
            lines.extend(self._generate_cpp_state_restore())

        lines.extend([
            '}',
            '',
            'bool DynamicPreprocessor::next(Row& out) {',
        ])

        if has_neighbor_filters:
            lines.extend(self._generate_cpp_next_with_neighbor_filters())
        else:
            lines.extend(self._generate_cpp_next_standard())

        lines.extend([
            f'}} // namespace {self.module_name}',
            '',
        ])

        return '\n'.join(lines)

    def _generate_cpp_state_vars(self) -> List[str]:
        lines = []
        stateful_transforms = self.config.get('stateful_transforms', [])
        for i, st in enumerate(stateful_transforms):
            st_type = st.get('type')
            if st_type == 'prefix_sum':
                lines.append(f'    double prefixSum_{i} = 0.0;')
            elif st_type == 'prefix_count':
                lines.append(f'    long long prefixCount_{i} = 0;')
            elif st_type == 'sliding_window':
                window_size = st.get('window_size', 3)
                lines.append(f'    std::deque<double> window_{i};')
                lines.append(f'    size_t windowSize_{i} = {window_size};')
            elif st_type == 'state_machine':
                initial = st.get('initial_state', 1)
                lines.append(f'    long long state_{i} = {initial};')
        return lines

    def _generate_cpp_state_load(self) -> List[str]:
        lines = []
        stateful_transforms = self.config.get('stateful_transforms', [])
        lines.append('        // Load state')
        for i, st in enumerate(stateful_transforms):
            st_type = st.get('type')
            if st_type == 'prefix_sum':
                lines.extend([
                    f'        {{',
                    f'            std::string key = "\\"prefix_sum_{i}\\"";',
                    f'            size_t pos = content.find(key);',
                    f'            if (pos != std::string::npos) {{',
                    f'                pos = content.find(":", pos);',
                    f'                if (pos != std::string::npos) prefixSum_{i} = std::stod(content.substr(pos + 1));',
                    f'            }}',
                    f'        }}',
                ])
            elif st_type == 'prefix_count':
                lines.extend([
                    f'        {{',
                    f'            std::string key = "\\"prefix_count_{i}\\"";',
                    f'            size_t pos = content.find(key);',
                    f'            if (pos != std::string::npos) {{',
                    f'                pos = content.find(":", pos);',
                    f'                if (pos != std::string::npos) prefixCount_{i} = std::stoll(content.substr(pos + 1));',
                    f'            }}',
                    f'        }}',
                ])
            elif st_type == 'sliding_window':
                lines.extend([
                    f'        {{',
                    f'            std::string key = "\\"window_{i}\\"";',
                    f'            size_t pos = content.find(key);',
                    f'            if (pos != std::string::npos) {{',
                    f'                pos = content.find("[", pos);',
                    f'                if (pos != std::string::npos) {{',
                    f'                    size_t end = content.find("]", pos);',
                    f'                    std::string arr = content.substr(pos + 1, end - pos - 1);',
                    f'                    std::stringstream ss(arr);',
                    f'                    std::string val;',
                    f'                    window_{i}.clear();',
                    f'                    while (std::getline(ss, val, \',\')) {{',
                    f'                        size_t start = val.find_first_not_of(" \\t\\n\\r");',
                    f'                        size_t end = val.find_last_not_of(" \\t\\n\\r");',
                    f'                        if (start != std::string::npos && end != std::string::npos) {{',
                    f'                            window_{i}.push_back(std::stod(val.substr(start, end - start + 1)));',
                    f'                        }}',
                    f'                    }}',
                    f'                }}',
                    f'            }}',
                    f'        }}',
                ])
            elif st_type == 'state_machine':
                initial = st.get('initial_state', 1)
                lines.extend([
                    f'        {{',
                    f'            std::string key = "\\"state_{i}\\"";',
                    f'            size_t pos = content.find(key);',
                    f'            if (pos != std::string::npos) {{',
                    f'                pos = content.find(":", pos);',
                    f'                if (pos != std::string::npos) state_{i} = std::stoll(content.substr(pos + 1));',
                    f'            }}',
                    f'        }}',
                ])
        return lines

    def _generate_cpp_state_save(self) -> List[str]:
        lines = []
        stateful_transforms = self.config.get('stateful_transforms', [])
        for i, st in enumerate(stateful_transforms):
            st_type = st.get('type')
            if i > 0:
                lines.append('        f << ", ";')
            if st_type == 'prefix_sum':
                lines.append(f'        f << "\\"prefix_sum_{i}\\": " << prefixSum_{i};')
            elif st_type == 'prefix_count':
                lines.append(f'        f << "\\"prefix_count_{i}\\": " << prefixCount_{i};')
            elif st_type == 'sliding_window':
                lines.extend([
                    f'        f << "\\"window_{i}\\": [";',
                    f'        for (size_t j = 0; j < window_{i}.size(); ++j) {{',
                    f'            if (j > 0) f << ", ";',
                    f'            f << window_{i}[j];',
                    f'        }}',
                    f'        f << "]";',
                ])
            elif st_type == 'state_machine':
                lines.append(f'        f << "\\"state_{i}\\": " << state_{i};')
        return lines

    def _generate_cpp_state_restore(self) -> List[str]:
        lines = []
        stateful_transforms = self.config.get('stateful_transforms', [])
        for i, st in enumerate(stateful_transforms):
            st_type = st.get('type')
            if st_type == 'prefix_sum':
                lines.append(f'    impl_->prefixSum_{i} = 0.0;')
            elif st_type == 'prefix_count':
                lines.append(f'    impl_->prefixCount_{i} = 0;')
            elif st_type == 'sliding_window':
                lines.append(f'    impl_->window_{i}.clear();')
            elif st_type == 'state_machine':
                initial = st.get('initial_state', 1)
                lines.append(f'    impl_->state_{i} = {initial};')
        return lines

    def _generate_cpp_should_keep_row(self) -> List[str]:
        conditions = self.config.get('filter_conditions', [])
        lines = [
            '    bool shouldKeepRow(const std::map<std::string, std::string>& row) {',
        ]
        if not conditions:
            lines.append('        return true;')
        else:
            for cond in conditions:
                col = cond['column']
                op = cond['operator']
                val = cond['value']
                if op == '!=':
                    if isinstance(val, bool):
                        lines.append(f'        if (row.count("{col}") && row.at("{col}") == "{"true" if val else "false"}") return false;')
                    elif isinstance(val, str):
                        lines.append(f'        if (row.count("{col}") && row.at("{col}") == "{val}") return false;')
                    else:
                        lines.append(f'        if (row.count("{col}") && row.at("{col}") == "{val}") return false;')
                elif op == '==':
                    if isinstance(val, bool):
                        lines.append(f'        if (!row.count("{col}") || row.at("{col}") != "{"true" if val else "false"}") return false;')
                    elif isinstance(val, str):
                        lines.append(f'        if (!row.count("{col}") || row.at("{col}") != "{val}") return false;')
                    else:
                        lines.append(f'        if (!row.count("{col}") || row.at("{col}") != "{val}") return false;')
                elif op in ('>', '>=', '<', '<='):
                    comp_ops = {'>': '<=', '>=': '<', '<': '>=', '<=': '>'}
                    lines.append(f'        if (row.count("{col}") && !row.at("{col}").empty()) {{')
                    lines.append(f'            try {{')
                    lines.append(f'                double v = std::stod(row.at("{col}"));')
                    lines.append(f'                if (v {comp_ops[op]} {val}) return false;')
                    lines.append(f'            }} catch (...) {{}}')
                    lines.append(f'        }}')
            lines.append('        return true;')
        lines.append('    }')
        return lines

    def _generate_cpp_transform_row(self) -> List[str]:
        transforms = self.config.get('column_transforms', {})
        stateful_transforms = self.config.get('stateful_transforms', [])
        stateful_output_cols = {st.get('output_column') for st in stateful_transforms}
        output_cols = self.config.get('output_columns', [])

        lines = [
            '    Row transformRow(const std::map<std::string, std::string>& rawRow) {',
            '        Row result;',
        ]

        for out_col in output_cols:
            if out_col in stateful_output_cols:
                continue

            transform = transforms.get(out_col, {'type': 'unknown'})
            t_type = transform.get('type')
            source = transform.get('source', out_col)

            if t_type in ('identity', 'copy'):
                lines.append(f'        if (rawRow.count("{source}")) result["{out_col}"] = parseValue(rawRow.at("{source}"));')
            elif t_type == 'constant':
                val = transform['value']
                if isinstance(val, bool):
                    lines.append(f'        result["{out_col}"] = Value({"true" if val else "false"});')
                elif isinstance(val, int):
                    lines.append(f'        result["{out_col}"] = Value({val}LL);')
                elif isinstance(val, float):
                    lines.append(f'        result["{out_col}"] = Value({val});')
                elif isinstance(val, str):
                    lines.append(f'        result["{out_col}"] = Value("{val}");')
                else:
                    lines.append(f'        result["{out_col}"] = Value();')
            elif t_type in ('strip', 'lower', 'upper', 'strip_lower', 'strip_upper'):
                lines.append(f'        if (rawRow.count("{source}")) {{')
                lines.append(f'            std::string s = rawRow.at("{source}");')
                if 'strip' in t_type:
                    lines.append('            size_t start = s.find_first_not_of(" \\t\\n\\r");')
                    lines.append('            size_t end = s.find_last_not_of(" \\t\\n\\r");')
                    lines.append('            if (start != std::string::npos) s = s.substr(start, end - start + 1);')
                    lines.append('            else s.clear();')
                if t_type == 'lower' or t_type == 'strip_lower':
                    lines.append('            std::transform(s.begin(), s.end(), s.begin(), ::tolower);')
                elif t_type == 'upper' or t_type == 'strip_upper':
                    lines.append('            std::transform(s.begin(), s.end(), s.begin(), ::toupper);')
                lines.append(f'            result["{out_col}"] = Value(s);')
                lines.append('        }')
            elif t_type == 'add_prefix':
                prefix = transform['prefix']
                lines.append(f'        if (rawRow.count("{source}")) {{')
                lines.append(f'            std::string s = rawRow.at("{source}");')
                lines.append(f'            result["{out_col}"] = Value("{prefix}" + s);')
                lines.append('        }')
            elif t_type == 'add_suffix':
                suffix = transform['suffix']
                lines.append(f'        if (rawRow.count("{source}")) {{')
                lines.append(f'            std::string s = rawRow.at("{source}");')
                lines.append(f'            result["{out_col}"] = Value(s + "{suffix}");')
                lines.append('        }')
            elif t_type == 'linear':
                a, b = transform['a'], transform['b']
                lines.append(f'        if (rawRow.count("{source}") && !rawRow.at("{source}").empty()) {{')
                lines.append(f'            try {{')
                lines.append(f'                double v = std::stod(rawRow.at("{source}"));')
                lines.append(f'                result["{out_col}"] = Value({a} * v + {b});')
                lines.append(f'            }} catch (...) {{}}')
                lines.append('        }')
            elif t_type != 'stateful':
                lines.append(f'        if (rawRow.count("{out_col}")) result["{out_col}"] = parseValue(rawRow.at("{out_col}"));')

        # Stateful transforms
        for i, st in enumerate(stateful_transforms):
            st_type = st.get('type')
            out_col = st.get('output_column')
            source = st.get('source')
            a = st.get('a', 1.0)
            b = st.get('b', 0.0)

            if st_type == 'prefix_sum':
                lines.extend([
                    f'        if (rawRow.count("{source}") && !rawRow.at("{source}").empty()) {{',
                    f'            try {{ prefixSum_{i} += std::stod(rawRow.at("{source}")); }} catch (...) {{}}',
                    '        }',
                    f'        result["{out_col}"] = Value({a} * prefixSum_{i} + {b});',
                ])
            elif st_type == 'prefix_count':
                condition = st.get('condition')
                if condition == 'not_null':
                    lines.extend([
                        f'        if (rawRow.count("{source}") && !rawRow.at("{source}").empty()) {{',
                        f'            prefixCount_{i}++;',
                        '        }',
                    ])
                elif condition == 'positive':
                    lines.extend([
                        f'        if (rawRow.count("{source}") && !rawRow.at("{source}").empty()) {{',
                        f'            try {{ if (std::stod(rawRow.at("{source}")) > 0) prefixCount_{i}++; }} catch (...) {{}}',
                        '        }',
                    ])
                elif condition == 'negative':
                    lines.extend([
                        f'        if (rawRow.count("{source}") && !rawRow.at("{source}").empty()) {{',
                        f'            try {{ if (std::stod(rawRow.at("{source}")) < 0) prefixCount_{i}++; }} catch (...) {{}}',
                        '        }',
                    ])
                elif condition == 'true':
                    lines.extend([
                        f'        if (rawRow.count("{source}") && rawRow.at("{source}") == "true") {{',
                        f'            prefixCount_{i}++;',
                        '        }',
                    ])
                else:
                    lines.append(f'        prefixCount_{i}++;')
                lines.append(f'        result["{out_col}"] = Value({a} * prefixCount_{i} + {b});')
            elif st_type == 'sliding_window':
                window_size = st.get('window_size', 3)
                operation = st.get('operation', 'mean')
                lines.extend([
                    f'        if (rawRow.count("{source}") && !rawRow.at("{source}").empty()) {{',
                    f'            try {{',
                    f'                window_{i}.push_back(std::stod(rawRow.at("{source}")));',
                    f'                while (window_{i}.size() > windowSize_{i}) window_{i}.pop_front();',
                    f'            }} catch (...) {{}}',
                    '        }',
                ])
                if operation == 'sum':
                    lines.extend([
                        f'        double windowVal_{i} = 0.0;',
                        f'        for (double v : window_{i}) windowVal_{i} += v;',
                    ])
                elif operation == 'mean':
                    lines.extend([
                        f'        double windowVal_{i} = 0.0;',
                        f'        for (double v : window_{i}) windowVal_{i} += v;',
                        f'        if (!window_{i}.empty()) windowVal_{i} /= window_{i}.size();',
                    ])
                lines.append(f'        result["{out_col}"] = Value({a} * windowVal_{i} + {b});')
            elif st_type == 'state_machine':
                transitions = st.get('transitions', [])
                lines.extend([
                    f'        if (rawRow.count("{source}") && !rawRow.at("{source}").empty()) {{',
                    f'            try {{',
                    f'                double val_sm = std::stod(rawRow.at("{source}"));',
                ])
                for t in transitions:
                    threshold = t.get('threshold')
                    direction = t.get('direction')
                    target_state = t.get('target_state')
                    if target_state is not None:
                        if direction == 'up':
                            lines.extend([
                                f'                if (val_sm >= {threshold} && state_{i} < {target_state}) state_{i} = {target_state};',
                            ])
                        else:
                            lines.extend([
                                f'                if (val_sm < {threshold} && state_{i} < {target_state}) state_{i} = {target_state};',
                            ])
                lines.extend([
                    '            } catch (...) {}',
                    '        }',
                    f'        result["{out_col}"] = Value(state_{i});',
                ])

        lines.append('        return result;')
        lines.append('    }')
        return lines

    def _generate_cpp_neighbor_filter_methods(self) -> List[str]:
        neighbor_filters = self.config.get('neighbor_filters', [])
        lines = []
        for i, nf in enumerate(neighbor_filters):
            nf_type = nf.get('type')
            if nf_type == 'next_row_condition':
                col = nf.get('column')
                val = nf.get('value')
                if isinstance(val, bool):
                    val_str = 'true' if val else 'false'
                elif isinstance(val, str):
                    val_str = val
                else:
                    val_str = str(val)
                lines.extend([
                    f'    bool checkNeighborFilter_{i}(const std::map<std::string, std::string>& current,',
                    f'                                const std::map<std::string, std::string>* next) {{',
                    f'        if (!next) return true;',
                    f'        if (next->count("{col}") && next->at("{col}") == "{val_str}") return false;',
                    '        return true;',
                    '    }',
                ])
            elif nf_type == 'consecutive_duplicate':
                col = nf.get('column')
                lines.extend([
                    f'    bool checkNeighborFilter_{i}(const std::map<std::string, std::string>& current,',
                    f'                                const std::map<std::string, std::string>* prev) {{',
                    f'        if (!prev) return true;',
                    f'        if (prev->count("{col}") && current.count("{col}") &&',
                    f'            prev->at("{col}") == current.at("{col}")) return false;',
                    '        return true;',
                    '    }',
                ])
        return lines

    def _generate_cpp_next_standard(self) -> List[str]:
        lines = [
            '    std::string cachePath = impl_->getCachePath(impl_->currentPath);',
            '    size_t bufferCount = 0;',
            '',
            '    while (impl_->currentIdx < impl_->rawRows.size()) {',
            '        if (impl_->currentIdx < impl_->processedRows) {',
            '            impl_->currentIdx++;',
            '            continue;',
            '        }',
            '',
            '        const auto& rawRow = impl_->rawRows[impl_->currentIdx];',
            '        impl_->currentIdx++;',
            '        impl_->processedRows = impl_->currentIdx;',
            '',
            '        if (!impl_->shouldKeepRow(rawRow)) {',
            '            impl_->saveCache(cachePath);',
            '            continue;',
            '        }',
            '',
            '        out = impl_->transformRow(rawRow);',
            '        impl_->cachedRows.push_back(out);',
            '        bufferCount++;',
            '',
            '        if (bufferCount >= impl_->buffer) {',
            '            impl_->saveCache(cachePath);',
            '            impl_->cachedRows.clear();',
            '            bufferCount = 0;',
            '        }',
            '',
            '        return true;',
            '    }',
            '',
            '    if (bufferCount > 0) {',
            '        impl_->saveCache(cachePath);',
            '        impl_->cachedRows.clear();',
            '    }',
            '',
            '    return false;',
            '}',
        ]
        return lines

    def _generate_cpp_next_with_neighbor_filters(self) -> List[str]:
        neighbor_filters = self.config.get('neighbor_filters', [])
        lines = [
            '    std::string cachePath = impl_->getCachePath(impl_->currentPath);',
            '    size_t bufferCount = 0;',
            '',
            '    // Store pending row and prev row for neighbor filtering',
            '    std::map<std::string, std::string> pendingRow;',
            '    bool hasPending = false;',
            '    size_t pendingIdx = 0;',
            '    std::map<std::string, std::string> prevRow;',
            '    bool hasPrev = false;',
            '',
            '    // Replay to current state',
            '    for (size_t i = 0; i < impl_->currentIdx && i < impl_->rawRows.size(); ++i) {',
            '        if (hasPending && pendingIdx == i - 1) {',
            '            const auto& next = impl_->rawRows[i];',
            '            bool keep = true;',
        ]

        for i, nf in enumerate(neighbor_filters):
            nf_type = nf.get('type')
            if nf_type == 'next_row_condition':
                lines.append(f'            keep = keep && impl_->checkNeighborFilter_{i}(pendingRow, &next);')
            elif nf_type == 'consecutive_duplicate':
                lines.append(f'            keep = keep && impl_->checkNeighborFilter_{i}(pendingRow, hasPrev ? &prevRow : nullptr);')

        lines.extend([
            '            if (keep && impl_->shouldKeepRow(pendingRow)) { /* row was kept */ }',
            '            hasPending = false;',
            '        }',
            '        prevRow = impl_->rawRows[i];',
            '        hasPrev = true;',
            '        pendingRow = impl_->rawRows[i];',
            '        hasPending = true;',
            '        pendingIdx = i;',
            '    }',
            '',
            '    // Continue processing',
            '    while (impl_->currentIdx < impl_->rawRows.size()) {',
            '        if (impl_->currentIdx < impl_->processedRows) {',
            '            impl_->currentIdx++;',
            '            continue;',
            '        }',
            '',
            '        const auto& rawRow = impl_->rawRows[impl_->currentIdx];',
            '',
            '        // Process pending row if exists',
            '        if (hasPending) {',
            '            bool keep = true;',
        ])

        for i, nf in enumerate(neighbor_filters):
            nf_type = nf.get('type')
            if nf_type == 'next_row_condition':
                lines.append(f'            keep = keep && impl_->checkNeighborFilter_{i}(pendingRow, &rawRow);')
            elif nf_type == 'consecutive_duplicate':
                lines.append(f'            keep = keep && impl_->checkNeighborFilter_{i}(pendingRow, hasPrev ? &prevRow : nullptr);')

        lines.extend([
            '            if (keep && impl_->shouldKeepRow(pendingRow)) {',
            '                out = impl_->transformRow(pendingRow);',
            '                impl_->cachedRows.push_back(out);',
            '                bufferCount++;',
            '                impl_->processedRows = impl_->currentIdx;',
            '                impl_->saveCache(cachePath);',
            '',
            '                if (bufferCount >= impl_->buffer) {',
            '                    impl_->cachedRows.clear();',
            '                    bufferCount = 0;',
            '                }',
            '',
            '                pendingRow = rawRow;',
            '                pendingIdx = impl_->currentIdx;',
            '                prevRow = rawRow;',
            '                hasPrev = true;',
            '                impl_->currentIdx++;',
            '                hasPending = true;',
            '                return true;',
            '            }',
            '            hasPending = false;',
            '        }',
            '',
            '        // Set current as pending',
            '        pendingRow = rawRow;',
            '        pendingIdx = impl_->currentIdx;',
            '        prevRow = hasPrev ? prevRow : rawRow;',
            '        hasPrev = true;',
            '        hasPending = true;',
            '        impl_->currentIdx++;',
            '    }',
            '',
            '    // Handle last pending row',
            '    if (hasPending) {',
            '        bool keep = true;',
        ])

        for i, nf in enumerate(neighbor_filters):
            nf_type = nf.get('type')
            if nf_type == 'next_row_condition':
                lines.append(f'        keep = keep && impl_->checkNeighborFilter_{i}(pendingRow, nullptr);')
            elif nf_type == 'consecutive_duplicate':
                lines.append(f'        keep = keep && impl_->checkNeighborFilter_{i}(pendingRow, hasPrev ? &prevRow : nullptr);')

        lines.extend([
            '        if (keep && impl_->shouldKeepRow(pendingRow)) {',
            '            out = impl_->transformRow(pendingRow);',
            '            impl_->cachedRows.push_back(out);',
            '            impl_->processedRows = impl_->currentIdx;',
            '            impl_->saveCache(cachePath);',
            '            hasPending = false;',
            '            return true;',
            '        }',
            '        hasPending = false;',
            '    }',
            '',
            '    if (bufferCount > 0) {',
            '        impl_->saveCache(cachePath);',
            '        impl_->cachedRows.clear();',
            '    }',
            '',
            '    return false;',
            '}',
        ])
        return lines


# =============================================================================
# Rust Code Generator
# =============================================================================

class RustCodeGenerator:
    """Generates Rust crate code from transformation config."""

    def __init__(self, module_name: str, config: Dict, file_ext: str):
        self.module_name = module_name
        self.config = config
        self.file_ext = file_ext

    def generate(self) -> Dict[str, str]:
        return {
            'Cargo.toml': self._generate_cargo_toml(),
            'src/lib.rs': self._generate_lib_rs(),
        }

    def _generate_cargo_toml(self) -> str:
        lines = [
            '[package]',
            f'name = "{self.module_name}"',
            'version = "0.1.0"',
            'edition = "2021"',
            '',
            '[lib]',
            'name = "' + self.module_name + '"',
            'path = "src/lib.rs"',
            '',
            '[dependencies]',
            'serde = { version = "1.0", features = ["derive"] }',
            'serde_json = "1.0"',
            '',
        ]
        return '\n'.join(lines)

    def _generate_lib_rs(self) -> str:
        has_stateful = bool(self.config.get('stateful_transforms'))
        has_neighbor_filters = bool(self.config.get('neighbor_filters'))

        lines = [
            '//! Dynamic Preprocessor - Streaming data processor with caching support.',
            '',
            'use std::collections::BTreeMap;',
            'use std::fs::{self, File};',
            'use std::io::{BufRead, BufReader, Read, Write};',
            'use std::path::PathBuf;',
            'use serde::{Deserialize, Serialize};',
            '',
            'pub mod api {',
            '    use std::collections::BTreeMap;',
            '    use std::io;',
            '    use std::path::Path;',
            '',
            '    /// Represents a cell value in a row.',
            '    #[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]',
            '    #[serde(untagged)]',
            '    pub enum Value {',
            '        Null,',
            '        Bool(bool),',
            '        Int(i64),',
            '        Float(f64),',
            '        Str(String),',
            '    }',
            '',
            '    impl Value {',
            '        pub fn is_null(&self) -> bool {',
            '            matches!(self, Value::Null)',
            '        }',
            '    }',
            '',
            '    impl Default for Value {',
            '        fn default() -> Self {',
            '            Value::Null',
            '        }',
            '    }',
            '',
            '    impl From<bool> for Value {',
            '        fn from(v: bool) -> Self { Value::Bool(v) }',
            '    }',
            '',
            '    impl From<i64> for Value {',
            '        fn from(v: i64) -> Self { Value::Int(v) }',
            '    }',
            '',
            '    impl From<i32> for Value {',
            '        fn from(v: i32) -> Self { Value::Int(v as i64) }',
            '    }',
            '',
            '    impl From<f64> for Value {',
            '        fn from(v: f64) -> Self { Value::Float(v) }',
            '    }',
            '',
            '    impl From<String> for Value {',
            '        fn from(v: String) -> Self { Value::Str(v) }',
            '    }',
            '',
            '    impl From<&str> for Value {',
            '        fn from(v: &str) -> Self { Value::Str(v.to_string()) }',
            '    }',
            '',
            '    pub type Row = BTreeMap<String, Value>;',
            '',
        ]

        # Generate state structures
        if has_stateful:
            lines.extend(self._generate_rust_state_structs())

        lines.extend([
            '    /// Streaming preprocessor with caching and resuming support.',
            '    pub struct DynamicPreprocessor {',
            '        buffer: usize,',
            '        cache_dir: Option<String>,',
            '        current_path: Option<String>,',
            '        raw_rows: Vec<BTreeMap<String, String>>,',
            '        current_idx: usize,',
            '        processed_rows: usize,',
            '        cached_rows: Vec<Row>,',
            '        buffer_count: usize,',
        ])

        if has_stateful:
            lines.extend(self._generate_rust_state_fields())

        if has_neighbor_filters:
            lines.extend([
                '        pending_row: Option<BTreeMap<String, String>>,',
                '        pending_idx: Option<usize>,',
                '        prev_row: Option<BTreeMap<String, String>>,',
            ])

        lines.extend([
            '    }',
            '',
        ])

        # Generate impl block
        lines.extend(self._generate_rust_impl())

        # impl Iterator trait
        lines.extend([
            '',
            '    impl Iterator for DynamicPreprocessor {',
            '        type Item = Row;',
            '',
            '        fn next(&mut self) -> Option<Self::Item> {',
            '            self.next_row()',
            '        }',
            '    }',
        ])

        # MD5 hash function for cache keys
        lines.extend([
            '',
            '    // MD5 hash function for cache keys',
            '    fn md5_hash(data: &[u8]) -> u128 {',
            '        let mut hash: u128 = 5381;',
            '        for &b in data {',
            '            hash = ((hash << 5).wrapping_add(hash)) ^ b as u128;',
            '        }',
            '        for &b in data {',
            '            hash = ((hash << 5).wrapping_add(hash)) ^ b as u128;',
            '        }',
            '        hash',
            '    }',
        ])

        lines.extend([
            '}  // mod api',
            '',
        ])

        return '\n'.join(lines)

    def _generate_rust_state_structs(self) -> List[str]:
        lines = []
        stateful_transforms = self.config.get('stateful_transforms', [])
        for i, st in enumerate(stateful_transforms):
            st_type = st.get('type')
            if st_type == 'prefix_sum':
                lines.extend([
                    f'    #[derive(Clone, Debug, Serialize, Deserialize)]',
                    f'    struct PrefixSumState_{i} {{',
                    f'        value: f64,',
                    f'    }}',
                    '',
                ])
            elif st_type == 'prefix_count':
                lines.extend([
                    f'    #[derive(Clone, Debug, Serialize, Deserialize)]',
                    f'    struct PrefixCountState_{i} {{',
                    f'        value: i64,',
                    f'    }}',
                    '',
                ])
            elif st_type == 'sliding_window':
                lines.extend([
                    f'    #[derive(Clone, Debug, Serialize, Deserialize)]',
                    f'    struct WindowState_{i} {{',
                    f'        values: Vec<f64>,',
                    f'        window_size: usize,',
                    f'    }}',
                    '',
                ])
            elif st_type == 'state_machine':
                lines.extend([
                    f'    #[derive(Clone, Debug, Serialize, Deserialize)]',
                    f'    struct StateMachineState_{i} {{',
                    f'        state: i64,',
                    f'    }}',
                    '',
                ])
        return lines

    def _generate_rust_state_fields(self) -> List[str]:
        lines = []
        stateful_transforms = self.config.get('stateful_transforms', [])
        for i, st in enumerate(stateful_transforms):
            st_type = st.get('type')
            if st_type == 'prefix_sum':
                lines.append(f'        prefix_sum_{i}: f64,')
            elif st_type == 'prefix_count':
                lines.append(f'        prefix_count_{i}: i64,')
            elif st_type == 'sliding_window':
                lines.append(f'        window_{i}: Vec<f64>,')
                window_size = st.get('window_size', 3)
                lines.append(f'        window_size_{i}: usize,')
            elif st_type == 'state_machine':
                lines.append(f'        state_{i}: i64,')
        return lines

    def _generate_rust_impl(self) -> List[str]:
        has_stateful = bool(self.config.get('stateful_transforms'))
        has_neighbor_filters = bool(self.config.get('neighbor_filters'))

        lines = [
            '    impl DynamicPreprocessor {',
            '        pub fn new(buffer: usize, cache_dir: Option<&str>) -> DynamicPreprocessor {',
            '            DynamicPreprocessor {',
            '                buffer,',
            '                cache_dir: cache_dir.map(|s| s.to_string()),',
            '                current_path: None,',
            '                raw_rows: Vec::new(),',
            '                current_idx: 0,',
            '                processed_rows: 0,',
            '                cached_rows: Vec::new(),',
            '                buffer_count: 0,',
        ]

        if has_stateful:
            stateful_transforms = self.config.get('stateful_transforms', [])
            for i, st in enumerate(stateful_transforms):
                st_type = st.get('type')
                if st_type == 'prefix_sum':
                    lines.append(f'                prefix_sum_{i}: 0.0,')
                elif st_type == 'prefix_count':
                    lines.append(f'                prefix_count_{i}: 0,')
                elif st_type == 'sliding_window':
                    window_size = st.get('window_size', 3)
                    lines.append(f'                window_{i}: Vec::new(),')
                    lines.append(f'                window_size_{i}: {window_size},')
                elif st_type == 'state_machine':
                    initial = st.get('initial_state', 1)
                    lines.append(f'                state_{i}: {initial},')

        if has_neighbor_filters:
            lines.extend([
                '                pending_row: None,',
                '                pending_idx: None,',
                '                prev_row: None,',
            ])

        lines.extend([
            '            }',
            '        }',
            '',
        ])

        # open method
        lines.extend(self._generate_rust_open_method())

        # Private helper methods
        lines.extend(self._generate_rust_helper_methods())

        # should_keep_row
        lines.extend(self._generate_rust_should_keep_row())

        # transform_row
        lines.extend(self._generate_rust_transform_row())

        # Iterator implementation
        lines.extend(self._generate_rust_iterator_impl())

        lines.extend([
            '    }',
            '',
        ])

        return lines

    def _generate_rust_open_method(self) -> List[str]:
        has_stateful = bool(self.config.get('stateful_transforms'))

        lines = [
            '        pub fn open<P: AsRef<std::path::Path>>(&mut self, path: P) -> io::Result<()> {',
            '            let path_str = path.as_ref().to_string_lossy().to_string();',
            '            self.current_path = Some(path_str.clone());',
            '            self.raw_rows.clear();',
            '            self.current_idx = 0;',
            '            self.processed_rows = 0;',
            '            self.cached_rows.clear();',
            '            self.buffer_count = 0;',
            '',
        ]

        if has_stateful:
            stateful_transforms = self.config.get('stateful_transforms', [])
            for i, st in enumerate(stateful_transforms):
                st_type = st.get('type')
                if st_type == 'prefix_sum':
                    lines.append(f'            self.prefix_sum_{i} = 0.0;')
                elif st_type == 'prefix_count':
                    lines.append(f'            self.prefix_count_{i} = 0;')
                elif st_type == 'sliding_window':
                    lines.append(f'            self.window_{i}.clear();')
                elif st_type == 'state_machine':
                    initial = st.get('initial_state', 1)
                    lines.append(f'            self.state_{i} = {initial};')

        lines.extend([
            '',
        ])

        if self.file_ext == 'csv':
            lines.extend([
                '            let file = File::open(&path)?;',
                '            let reader = BufReader::new(file);',
                '            let mut lines = reader.lines();',
                '',
                '            let headers: Vec<String> = if let Some(Ok(line)) = lines.next() {',
                '                line.split(\',\').map(|s| s.trim().to_string()).collect()',
                '            } else {',
                '                return Ok(());',
                '            };',
                '',
                '            for line in lines {',
                '                let line = line?;',
                '                if line.trim().is_empty() { continue; }',
                '                let values: Vec<&str> = line.split(\',\').collect();',
                '                let mut row = BTreeMap::new();',
                '                for (i, h) in headers.iter().enumerate() {',
                '                    let v = if i < values.len() { values[i].trim().to_string() } else { String::new() };',
                '                    row.insert(h.clone(), v);',
                '                }',
                '                self.raw_rows.push(row);',
                '            }',
            ])
        elif self.file_ext == 'tsv':
            lines.extend([
                '            let file = File::open(&path)?;',
                '            let reader = BufReader::new(file);',
                '            let mut lines = reader.lines();',
                '',
                '            let headers: Vec<String> = if let Some(Ok(line)) = lines.next() {',
                '                line.split(\'\\t\').map(|s| s.trim().to_string()).collect()',
                '            } else {',
                '                return Ok(());',
                '            };',
                '',
                '            for line in lines {',
                '                let line = line?;',
                '                if line.trim().is_empty() { continue; }',
                '                let values: Vec<&str> = line.split(\'\\t\').collect();',
                '                let mut row = BTreeMap::new();',
                '                for (i, h) in headers.iter().enumerate() {',
                '                    let v = if i < values.len() { values[i].trim().to_string() } else { String::new() };',
                '                    row.insert(h.clone(), v);',
                '                }',
                '                self.raw_rows.push(row);',
                '            }',
            ])
        elif self.file_ext == 'jsonl':
            lines.extend([
                '            let file = File::open(&path)?;',
                '            let reader = BufReader::new(file);',
                '',
                '            for line in reader.lines() {',
                '                let line = line?;',
                '                if line.trim().is_empty() { continue; }',
                '                if let Ok(obj) = serde_json::from_str::<serde_json::Value>(&line) {',
                '                    if let Some(map) = obj.as_object() {',
                '                        let mut row = BTreeMap::new();',
                '                        for (k, v) in map {',
                '                            let v_str = match v {',
                '                                serde_json::Value::Null => String::new(),',
                '                                serde_json::Value::Bool(b) => b.to_string(),',
                '                                serde_json::Value::Number(n) => n.to_string(),',
                '                                serde_json::Value::String(s) => s.clone(),',
                '                                _ => v.to_string(),',
                '                            };',
                '                            row.insert(k.clone(), v_str);',
                '                        }',
                '                        self.raw_rows.push(row);',
                '                    }',
                '                }',
                '            }',
            ])
        elif self.file_ext == 'json':
            lines.extend([
                '            let mut content = String::new();',
                '            File::open(&path)?.read_to_string(&mut content)?;',
                '',
                '            if let Ok(arr) = serde_json::from_str::<serde_json::Value>(&content) {',
                '                if let Some(items) = arr.as_array() {',
                '                    for item in items {',
                '                        if let Some(map) = item.as_object() {',
                '                            let mut row = BTreeMap::new();',
                '                            for (k, v) in map {',
                '                                let v_str = match v {',
                '                                    serde_json::Value::Null => String::new(),',
                '                                    serde_json::Value::Bool(b) => b.to_string(),',
                '                                    serde_json::Value::Number(n) => n.to_string(),',
                '                                    serde_json::Value::String(s) => s.clone(),',
                '                                    _ => v.to_string(),',
                '                                };',
                '                                row.insert(k.clone(), v_str);',
                '                            }',
                '                            self.raw_rows.push(row);',
                '                        }',
                '                    }',
                '                }',
                '            }',
            ])

        lines.extend([
            '',
            '            // Load cache if exists',
            '            if let Some(cache_path) = self.get_cache_path() {',
            '                self.load_cache(&cache_path);',
            '            }',
            '',
            '            Ok(())',
            '        }',
            '',
        ])

        return lines

    def _generate_rust_helper_methods(self) -> List[str]:
        has_stateful = bool(self.config.get('stateful_transforms'))

        lines = [
            '        fn get_cache_path(&self) -> Option<PathBuf> {',
            '            let cache_dir = self.cache_dir.as_ref()?;',
            '            let path = self.current_path.as_ref()?;',
            '            let _ = fs::create_dir_all(cache_dir);',
            '            let hash = format!("{:x}", md5_hash(path.as_bytes()));',
            '            Some(PathBuf::from(cache_dir).join(format!("{}.json", hash)))',
            '        }',
            '',
            '        fn load_cache(&mut self, cache_path: &PathBuf) {',
            '            if !cache_path.exists() { return; }',
            '            if let Ok(content) = fs::read_to_string(cache_path) {',
            '                if let Ok(cache) = serde_json::from_str::<serde_json::Value>(&content) {',
            '                    if let Some(obj) = cache.as_object() {',
            '                        if let Some(v) = obj.get("processed_rows").and_then(|v| v.as_u64()) {',
            '                            self.processed_rows = v as usize;',
            '                        }',
            '                        if let Some(state) = obj.get("state").and_then(|v| v.as_object()) {',
        ]

        if has_stateful:
            stateful_transforms = self.config.get('stateful_transforms', [])
            for i, st in enumerate(stateful_transforms):
                st_type = st.get('type')
                if st_type == 'prefix_sum':
                    lines.extend([
                        f'                            if let Some(v) = state.get("prefix_sum_{i}").and_then(|v| v.as_f64()) {{',
                        f'                                self.prefix_sum_{i} = v;',
                        '                            }',
                    ])
                elif st_type == 'prefix_count':
                    lines.extend([
                        f'                            if let Some(v) = state.get("prefix_count_{i}").and_then(|v| v.as_i64()) {{',
                        f'                                self.prefix_count_{i} = v;',
                        '                            }',
                    ])
                elif st_type == 'sliding_window':
                    lines.extend([
                        f'                            if let Some(v) = state.get("window_{i}").and_then(|v| v.as_array()) {{',
                        f'                                self.window_{i} = v.iter().filter_map(|x| x.as_f64()).collect();',
                        '                            }',
                    ])
                elif st_type == 'state_machine':
                    initial = st.get('initial_state', 1)
                    lines.extend([
                        f'                            if let Some(v) = state.get("state_{i}").and_then(|v| v.as_i64()) {{',
                        f'                                self.state_{i} = v;',
                        '                            }',
                    ])

        lines.extend([
            '                        }',
            '                    }',
            '                }',
            '            }',
            '        }',
            '',
            '        fn save_cache(&mut self, cache_path: &PathBuf) {',
            '            let mut state_obj = serde_json::Map::new();',
        ])

        if has_stateful:
            stateful_transforms = self.config.get('stateful_transforms', [])
            for i, st in enumerate(stateful_transforms):
                st_type = st.get('type')
                if st_type == 'prefix_sum':
                    lines.append(f'            state_obj.insert("prefix_sum_{i}".to_string(), serde_json::json!(self.prefix_sum_{i}));')
                elif st_type == 'prefix_count':
                    lines.append(f'            state_obj.insert("prefix_count_{i}".to_string(), serde_json::json!(self.prefix_count_{i}));')
                elif st_type == 'sliding_window':
                    lines.append(f'            state_obj.insert("window_{i}".to_string(), serde_json::json!(self.window_{i}));')
                elif st_type == 'state_machine':
                    lines.append(f'            state_obj.insert("state_{i}".to_string(), serde_json::json!(self.state_{i}));')

        lines.extend([
            '',
            '            let cache = serde_json::json!({',
            '                "processed_rows": self.processed_rows,',
            '                "rows": self.cached_rows,',
            '                "state": state_obj',
            '            });',
            '',
            '            let _ = fs::write(cache_path, serde_json::to_string_pretty(&cache).unwrap_or_default());',
            '        }',
            '',
            '        fn parse_value(s: &str) -> Value {',
            '            if s.is_empty() { return Value::Null; }',
            '            if s == "true" { return Value::Bool(true); }',
            '            if s == "false" { return Value::Bool(false); }',
            '            if let Ok(v) = s.parse::<i64>() { return Value::Int(v); }',
            '            if let Ok(v) = s.parse::<f64>() { return Value::Float(v); }',
            '            Value::Str(s.to_string())',
            '        }',
            '',
        ])

        return lines

    def _generate_rust_should_keep_row(self) -> List[str]:
        conditions = self.config.get('filter_conditions', [])

        lines = [
            '        fn should_keep_row(&self, row: &BTreeMap<String, String>) -> bool {',
        ]

        if not conditions:
            lines.append('            true')
        else:
            for cond in conditions:
                col = cond['column']
                op = cond['operator']
                val = cond['value']
                if op == '!=':
                    if isinstance(val, bool):
                        val_str = 'true' if val else 'false'
                        lines.append(f'            if row.get("{col}").map(|v| v.as_str()) == Some("{val_str}") {{ return false; }}')
                    else:
                        lines.append(f'            if row.get("{col}").map(|v| v.as_str()) == Some("{val}") {{ return false; }}')
                elif op == '==':
                    if isinstance(val, bool):
                        val_str = 'true' if val else 'false'
                        lines.append(f'            if row.get("{col}").map(|v| v.as_str()) != Some("{val_str}") {{ return false; }}')
                    else:
                        lines.append(f'            if row.get("{col}").map(|v| v.as_str()) != Some("{val}") {{ return false; }}')
                elif op in ('>', '>=', '<', '<='):
                    comp_ops = {'>': '<=', '>=': '<', '<': '>=', '<=': '>'}
                    lines.extend([
                        f'            if let Some(v) = row.get("{col}") {{',
                        f'                if let Ok(n) = v.parse::<f64>() {{',
                        f'                    if n {comp_ops[op]} {val} {{ return false; }}',
                        '                }',
                        '            }',
                    ])
            lines.append('            true')

        lines.extend([
            '        }',
            '',
        ])

        return lines

    def _generate_rust_transform_row(self) -> List[str]:
        transforms = self.config.get('column_transforms', {})
        stateful_transforms = self.config.get('stateful_transforms', [])
        stateful_output_cols = {st.get('output_column') for st in stateful_transforms}
        output_cols = self.config.get('output_columns', [])

        lines = [
            '        fn transform_row(&mut self, row: &BTreeMap<String, String>) -> Row {',
            '            let mut result = Row::new();',
            '',
        ]

        for out_col in output_cols:
            if out_col in stateful_output_cols:
                continue

            transform = transforms.get(out_col, {'type': 'unknown'})
            t_type = transform.get('type')
            source = transform.get('source', out_col)

            if t_type in ('identity', 'copy'):
                lines.extend([
                    f'            if let Some(v) = row.get("{source}") {{',
                    f'                result.insert("{out_col}".to_string(), Self::parse_value(v));',
                    '            }',
                ])
            elif t_type == 'constant':
                val = transform['value']
                if isinstance(val, bool):
                    lines.append(f'            result.insert("{out_col}".to_string(), Value::Bool({"true" if val else "false"}));')
                elif isinstance(val, int):
                    lines.append(f'            result.insert("{out_col}".to_string(), Value::Int({val}i64));')
                elif isinstance(val, float):
                    lines.append(f'            result.insert("{out_col}".to_string(), Value::Float({val}f64));')
                elif isinstance(val, str):
                    lines.append(f'            result.insert("{out_col}".to_string(), Value::Str("{val}".to_string()));')
            elif t_type in ('strip', 'lower', 'upper', 'strip_lower', 'strip_upper'):
                lines.extend([
                    f'            if let Some(v) = row.get("{source}") {{',
                    '                let mut s = v.clone();',
                ])
                if 'strip' in t_type:
                    lines.append('                s = s.trim().to_string();')
                if t_type == 'lower' or t_type == 'strip_lower':
                    lines.append('                s = s.to_lowercase();')
                elif t_type == 'upper' or t_type == 'strip_upper':
                    lines.append('                s = s.to_uppercase();')
                lines.extend([
                    f'                result.insert("{out_col}".to_string(), Value::Str(s));',
                    '            }',
                ])
            elif t_type == 'add_prefix':
                prefix = transform['prefix']
                lines.extend([
                    f'            if let Some(v) = row.get("{source}") {{',
                    f'                result.insert("{out_col}".to_string(), Value::Str(format!("{prefix}{{}}", v)));',
                    '            }',
                ])
            elif t_type == 'add_suffix':
                suffix = transform['suffix']
                lines.extend([
                    f'            if let Some(v) = row.get("{source}") {{',
                    f'                result.insert("{out_col}".to_string(), Value::Str(format!("{{}}{suffix}", v)));',
                    '            }',
                ])
            elif t_type == 'linear':
                a, b = transform['a'], transform['b']
                lines.extend([
                    f'            if let Some(v) = row.get("{source}") {{',
                    f'                if let Ok(n) = v.parse::<f64>() {{',
                    f'                    result.insert("{out_col}".to_string(), Value::Float({a}f64 * n + {b}f64));',
                    '                }',
                    '            }',
                ])
            elif t_type != 'stateful':
                lines.extend([
                    f'            if let Some(v) = row.get("{out_col}") {{',
                    f'                result.insert("{out_col}".to_string(), Self::parse_value(v));',
                    '            }',
                ])

        # Stateful transforms
        for i, st in enumerate(stateful_transforms):
            st_type = st.get('type')
            out_col = st.get('output_column')
            source = st.get('source')
            a = st.get('a', 1.0)
            b = st.get('b', 0.0)

            if st_type == 'prefix_sum':
                lines.extend([
                    f'            if let Some(v) = row.get("{source}") {{',
                    f'                if let Ok(n) = v.parse::<f64>() {{',
                    f'                    self.prefix_sum_{i} += n;',
                    '                }',
                    '            }',
                    f'            result.insert("{out_col}".to_string(), Value::Float({a}f64 * self.prefix_sum_{i} + {b}f64));',
                ])
            elif st_type == 'prefix_count':
                condition = st.get('condition')
                if condition == 'not_null':
                    lines.extend([
                        f'            if let Some(v) = row.get("{source}") {{',
                        f'                if !v.is_empty() {{',
                        f'                    self.prefix_count_{i} += 1;',
                        '                }',
                        '            }',
                    ])
                elif condition == 'positive':
                    lines.extend([
                        f'            if let Some(v) = row.get("{source}") {{',
                        f'                if let Ok(n) = v.parse::<f64>() {{',
                        f'                    if n > 0.0 {{ self.prefix_count_{i} += 1; }}',
                        '                }',
                        '            }',
                    ])
                elif condition == 'negative':
                    lines.extend([
                        f'            if let Some(v) = row.get("{source}") {{',
                        f'                if let Ok(n) = v.parse::<f64>() {{',
                        f'                    if n < 0.0 {{ self.prefix_count_{i} += 1; }}',
                        '                }',
                        '            }',
                    ])
                elif condition == 'true':
                    lines.extend([
                        f'            if row.get("{source}").map(|v| v.as_str()) == Some("true") {{',
                        f'                self.prefix_count_{i} += 1;',
                        '            }',
                    ])
                else:
                    lines.append(f'            self.prefix_count_{i} += 1;')
                lines.append(f'            result.insert("{out_col}".to_string(), Value::Int({a} as i64 * self.prefix_count_{i} + {b} as i64));')
            elif st_type == 'sliding_window':
                window_size = st.get('window_size', 3)
                operation = st.get('operation', 'mean')
                lines.extend([
                    f'            if let Some(v) = row.get("{source}") {{',
                    f'                if let Ok(n) = v.parse::<f64>() {{',
                    f'                    self.window_{i}.push(n);',
                    f'                    while self.window_{i}.len() > self.window_size_{i} {{',
                    f'                        self.window_{i}.remove(0);',
                    '                    }',
                    '                }',
                    '            }',
                ])
                if operation == 'sum':
                    lines.extend([
                        f'            let window_val_{i}: f64 = self.window_{i}.iter().sum();',
                    ])
                elif operation == 'mean':
                    lines.extend([
                        f'            let window_val_{i} = if self.window_{i}.is_empty() {{ 0.0 }} else {{',
                        f'                self.window_{i}.iter().sum::<f64>() / self.window_{i}.len() as f64',
                        '            };',
                    ])
                lines.append(f'            result.insert("{out_col}".to_string(), Value::Float({a}f64 * window_val_{i} + {b}f64));')
            elif st_type == 'state_machine':
                transitions = st.get('transitions', [])
                lines.extend([
                    f'            if let Some(v) = row.get("{source}") {{',
                    f'                if let Ok(n) = v.parse::<f64>() {{',
                ])
                for t in transitions:
                    threshold = t.get('threshold')
                    direction = t.get('direction')
                    target_state = t.get('target_state')
                    if target_state is not None:
                        if direction == 'up':
                            lines.extend([
                                f'                    if n >= {threshold}f64 && self.state_{i} < {target_state} {{',
                                f'                        self.state_{i} = {target_state};',
                                '                    }',
                            ])
                        else:
                            lines.extend([
                                f'                    if n < {threshold}f64 && self.state_{i} < {target_state} {{',
                                f'                        self.state_{i} = {target_state};',
                                '                    }',
                            ])
                lines.extend([
                    '                }',
                    '            }',
                    f'            result.insert("{out_col}".to_string(), Value::Int(self.state_{i}));',
                ])

        lines.extend([
            '            result',
            '        }',
            '',
        ])

        return lines

    def _generate_rust_iterator_impl(self) -> List[str]:
        lines = [
            '        fn next_row(&mut self) -> Option<Row> {',
            '            let cache_path = self.get_cache_path();',
            '',
            '            while self.current_idx < self.raw_rows.len() {',
            '                if self.current_idx < self.processed_rows {',
            '                    self.current_idx += 1;',
            '                    continue;',
            '                }',
            '',
            '                let raw_row = self.raw_rows[self.current_idx].clone();',
            '                self.current_idx += 1;',
            '                self.processed_rows = self.current_idx;',
            '',
            '                if !self.should_keep_row(&raw_row) {',
            '                    if let Some(ref p) = cache_path { self.save_cache(p); }',
            '                    continue;',
            '                }',
            '',
            '                let result = self.transform_row(&raw_row);',
            '                self.cached_rows.push(result.clone());',
            '                self.buffer_count += 1;',
            '',
            '                if self.buffer_count >= self.buffer {',
            '                    if let Some(ref p) = cache_path { self.save_cache(p); }',
            '                    self.cached_rows.clear();',
            '                    self.buffer_count = 0;',
            '                }',
            '',
            '                return Some(result);',
            '            }',
            '',
            '            if self.buffer_count > 0 {',
            '                if let Some(ref p) = cache_path { self.save_cache(p); }',
            '                self.cached_rows.clear();',
            '            }',
            '',
            '            None',
            '        }',
        ]

        return lines


# =============================================================================
# Main Entry Point
# =============================================================================

def find_sample_files(sample_dir: str) -> Tuple[str, str]:
    """Find input and output files in sample directory."""
    sample_path = Path(sample_dir)
    for ext in ['csv', 'tsv', 'jsonl', 'json']:
        potential_input = sample_path / ('input.' + ext)
        potential_output = sample_path / ('output.' + ext)
        if potential_input.exists() and potential_output.exists():
            return str(potential_input), str(potential_output)
    raise ValueError("Could not find matching input/output pair in " + sample_dir)


def main():
    parser = argparse.ArgumentParser(description='Dynamic Buffer Code Generator')
    parser.add_argument('module_name', help='Name of the generated module')
    parser.add_argument('--output', required=True, help='Output directory')
    parser.add_argument('--sample', required=True, help='Sample directory containing input/output files')
    parser.add_argument('--python', action='store_true', help='Generate Python module')
    parser.add_argument('--javascript', action='store_true', help='Generate JavaScript module')
    parser.add_argument('--cpp', action='store_true', help='Generate C++ module')
    parser.add_argument('--rust', action='store_true', help='Generate Rust crate')

    args = parser.parse_args()

    # Count how many language flags are set
    lang_flags = [args.python, args.javascript, args.cpp, args.rust]
    if sum(lang_flags) != 1:
        parser.error("Exactly one of --python, --javascript, --cpp, or --rust must be specified")

    input_file, output_file = find_sample_files(args.sample)
    file_ext = get_file_extension(input_file)
    input_rows, _ = parse_file(input_file)
    output_rows, _ = parse_file(output_file)

    inferrer = TransformationInferrer(input_rows, output_rows)
    config = inferrer.infer()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.python:
        generator = PythonCodeGenerator(args.module_name, config, file_ext)
        files = generator.generate()
        package_dir = output_dir / args.module_name
        package_dir.mkdir(parents=True, exist_ok=True)
        for filename, content in files.items():
            with open(package_dir / filename, 'w', encoding='utf-8') as f:
                f.write(content)
        print("Generated Python package:", package_dir)
    elif args.javascript:
        generator = JavaScriptCodeGenerator(args.module_name, config, file_ext)
        code = generator.generate()
        module_dir = output_dir / args.module_name
        module_dir.mkdir(parents=True, exist_ok=True)
        with open(module_dir / 'index.js', 'w', encoding='utf-8') as f:
            f.write(code)
        print("Generated JavaScript module:", module_dir)
    elif args.cpp:
        generator = CppCodeGenerator(args.module_name, config, file_ext)
        files = generator.generate()
        module_dir = output_dir / args.module_name
        module_dir.mkdir(parents=True, exist_ok=True)
        for filename, content in files.items():
            with open(module_dir / filename, 'w', encoding='utf-8') as f:
                f.write(content)
        print("Generated C++ module:", module_dir)
    elif args.rust:
        generator = RustCodeGenerator(args.module_name, config, file_ext)
        files = generator.generate()
        crate_dir = output_dir / args.module_name
        crate_dir.mkdir(parents=True, exist_ok=True)
        src_dir = crate_dir / 'src'
        src_dir.mkdir(parents=True, exist_ok=True)
        for filename, content in files.items():
            if filename == 'Cargo.toml':
                with open(crate_dir / filename, 'w', encoding='utf-8') as f:
                    f.write(content)
            else:
                # Strip leading src/ if present
                clean_name = filename
                if clean_name.startswith('src/'):
                    clean_name = clean_name[4:]
                with open(src_dir / clean_name, 'w', encoding='utf-8') as f:
                    f.write(content)
        print("Generated Rust crate:", crate_dir)


if __name__ == '__main__':
    main()
