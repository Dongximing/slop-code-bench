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
import math
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
    """Parse a string value to appropriate Python type."""
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

class TransformationInferrer:
    """Infers transformations from input/output sample data."""

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
        """Infer filter conditions, including neighbor-based filters."""
        if not self.output_rows:
            return

        kept_input_indices = set(self.row_mapping.values())
        dropped_input_indices = set(range(len(self.input_rows))) - kept_input_indices

        if not dropped_input_indices:
            return

        dropped_rows = [self.input_rows[i] for i in sorted(dropped_input_indices)]
        kept_rows = [self.input_rows[i] for i in sorted(kept_input_indices)]

        # First check for per-row filter conditions
        self.filter_conditions = self._find_distinguishing_conditions(kept_rows, dropped_rows)

        # Only check for neighbor-based filtering if per-row conditions don't fully explain dropped rows
        # A per-row condition "fully explains" dropped rows if every dropped row matches at least one condition
        per_row_explains_all = False
        if self.filter_conditions:
            per_row_explains_all = True
            for dropped_row in dropped_rows:
                matches_any = False
                for cond in self.filter_conditions:
                    if self._row_matches_condition(dropped_row, cond):
                        matches_any = True
                        break
                if not matches_any:
                    per_row_explains_all = False
                    break

        # Only infer neighbor filters if per-row conditions don't fully explain the drops
        if not per_row_explains_all:
            self.neighbor_filters = self._infer_neighbor_filters(dropped_input_indices, kept_input_indices)

    def _row_matches_condition(self, row: Dict[str, Any], cond: Dict[str, Any]) -> bool:
        """Check if a row matches a filter condition (meaning it would be dropped)."""
        col = cond.get('column')
        op = cond.get('operator')
        val = cond.get('value')
        row_val = row.get(col)

        if op == '!=':
            return row_val == val
        elif op == '==':
            return row_val != val
        elif op == '>' and isinstance(row_val, (int, float)):
            return row_val <= val
        elif op == '>=' and isinstance(row_val, (int, float)):
            return row_val < val
        elif op == '<' and isinstance(row_val, (int, float)):
            return row_val >= val
        elif op == '<=' and isinstance(row_val, (int, float)):
            return row_val > val
        return False

    def _infer_neighbor_filters(self, dropped_indices: Set[int], kept_indices: Set[int]) -> List[Dict[str, Any]]:
        """Infer neighbor-based filter patterns.

        Only returns filters that distinguish dropped rows from kept rows.
        A neighbor filter is only valid if ALL dropped rows have the pattern
        and NO kept rows have the pattern.
        """
        filters = []

        # Pattern 1: Drop row if next row has a specific condition
        # This is only valid if ALL dropped rows have a next row with this condition
        # and NO kept rows have a next row with this condition
        for col in self.input_cols:
            # Collect next-row values for dropped rows
            dropped_next_vals = []
            for dropped_idx in dropped_indices:
                next_idx = dropped_idx + 1
                if next_idx < len(self.input_rows):
                    dropped_next_vals.append(self.input_rows[next_idx].get(col))
                else:
                    dropped_next_vals.append(None)  # No next row

            # All dropped rows must have a next row with the same non-None value
            if None not in dropped_next_vals and len(set(dropped_next_vals)) == 1:
                candidate_val = dropped_next_vals[0]
                if candidate_val is not None:
                    # Check that NO kept rows have this pattern
                    kept_have_pattern = False
                    for kept_idx in kept_indices:
                        next_idx = kept_idx + 1
                        if next_idx < len(self.input_rows):
                            if self.input_rows[next_idx].get(col) == candidate_val:
                                kept_have_pattern = True
                                break

                    if not kept_have_pattern:
                        filters.append({
                            'type': 'next_row_condition',
                            'column': col,
                            'operator': '==',
                            'value': candidate_val,
                        })
                        return filters  # Return first valid pattern

        # Pattern 2: Drop row if previous row has same value (duplicate detection)
        for col in self.input_cols:
            dropped_are_dups = True
            for dropped_idx in dropped_indices:
                if dropped_idx > 0:
                    prev_row = self.input_rows[dropped_idx - 1]
                    curr_row = self.input_rows[dropped_idx]
                    if prev_row.get(col) != curr_row.get(col) or curr_row.get(col) is None:
                        dropped_are_dups = False
                        break
                else:
                    dropped_are_dups = False
                    break

            if dropped_are_dups:
                # Check that NO kept rows are duplicates of their previous row
                kept_are_dups = False
                for kept_idx in kept_indices:
                    if kept_idx > 0:
                        prev_row = self.input_rows[kept_idx - 1]
                        curr_row = self.input_rows[kept_idx]
                        if prev_row.get(col) == curr_row.get(col) and curr_row.get(col) is not None:
                            kept_are_dups = True
                            break

                if not kept_are_dups:
                    filters.append({
                        'type': 'consecutive_duplicate',
                        'column': col,
                    })
                    return filters

        return filters

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
                          'value': self._parse_filter_value(val)}]
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

    def _parse_filter_value(self, val: str) -> Any:
        if val.lower() == 'true':
            return True
        if val.lower() == 'false':
            return False
        try:
            return int(val)
        except ValueError:
            pass
        try:
            return float(val)
        except ValueError:
            pass
        return val

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

        if out_col in self.input_cols and self._check_exact_match(out_col, out_values):
            return {'type': 'identity', 'source': out_col}

        for in_col in self.input_cols:
            if self._check_column_copy(in_col, out_values):
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

    def _check_exact_match(self, col: str, out_values: List) -> bool:
        for out_idx, out_val in enumerate(out_values):
            if out_idx not in self.row_mapping:
                return False
            if self.input_rows[self.row_mapping[out_idx]].get(col) != out_val:
                return False
        return True

    def _check_column_copy(self, in_col: str, out_values: List) -> bool:
        for out_idx, out_val in enumerate(out_values):
            if out_idx not in self.row_mapping:
                return False
            if self.input_rows[self.row_mapping[out_idx]].get(in_col) != out_val:
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

        x1, x2 = in_values[0], in_values[1]
        y1, y2 = out_values[0], out_values[1]
        if x1 == x2:
            return {'type': 'constant', 'value': y1} if y1 == y2 else None
        a = (y2 - y1) / (x2 - x1)
        b = y1 - a * x1
        if any(abs(a * in_values[i] + b - out_values[i]) > 1e-9 for i in range(len(out_values))):
            return None
        return {'type': 'linear', 'source': in_col, 'a': a, 'b': b}

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
        if len(out_values) < 2:
            return None
        if not all(isinstance(v, (int, float)) for v in out_values):
            return None

        # Try each input column as source
        for in_col in self.input_cols:
            in_values = [self.input_rows[self.row_mapping[i]].get(in_col)
                        for i in range(len(out_values)) if i in self.row_mapping]

            if len(in_values) != len(out_values):
                continue

            if not all(isinstance(v, (int, float)) for v in in_values):
                continue

            # Compute prefix sums
            prefix_sums = []
            running_sum = 0
            for v in in_values:
                running_sum += v
                prefix_sums.append(running_sum)

            # Check if out_values = a * prefix_sums + b
            if len(out_values) >= 2:
                y1, y2 = out_values[0], out_values[1]
                s1, s2 = prefix_sums[0], prefix_sums[1]

                if s1 == s2:
                    if all(abs(out_values[i] - y1) < 1e-9 for i in range(len(out_values))):
                        # It's a constant, not a prefix sum
                        continue
                    continue

                a = (y2 - y1) / (s2 - s1)
                b = y1 - a * s1

                # Verify
                if all(abs(a * prefix_sums[i] + b - out_values[i]) < 1e-6 for i in range(len(out_values))):
                    return {
                        'type': 'prefix_sum',
                        'source': in_col,
                        'a': a,
                        'b': b,
                        'output_column': out_col,
                    }

        return None

    def _try_prefix_count(self, out_col: str, out_values: List) -> Optional[Dict[str, Any]]:
        """Try to detect prefix count pattern: out[i] = count up to and including i."""
        if len(out_values) < 2:
            return None
        if not all(isinstance(v, (int, float)) for v in out_values):
            return None

        # Check if out_values = row_index + 1 (simple row count)
        row_indices = list(range(len(out_values)))
        if all(abs(out_values[i] - (i + 1)) < 1e-9 for i in range(len(out_values))):
            return {
                'type': 'prefix_count',
                'source': None,
                'a': 1.0,
                'b': 0.0,
                'output_column': out_col,
            }

        # Check if out_values = a * count + b where count increments based on condition
        # Try each input column with condition checking
        for in_col in self.input_cols:
            in_values = [self.input_rows[self.row_mapping[i]].get(in_col)
                        for i in range(len(out_values)) if i in self.row_mapping]

            if len(in_values) != len(out_values):
                continue

            # Check if count increments when value meets certain conditions
            for condition_type in ['not_null', 'positive', 'negative', 'true']:
                counts = []
                count = 0
                for v in in_values:
                    if self._check_condition(v, condition_type):
                        count += 1
                    counts.append(count)

                if len(counts) >= 2:
                    y1, y2 = out_values[0], out_values[1]
                    c1, c2 = counts[0], counts[1]

                    if c1 == c2:
                        continue

                    a = (y2 - y1) / (c2 - c1)
                    b = y1 - a * c1

                    if all(abs(a * counts[i] + b - out_values[i]) < 1e-6 for i in range(len(out_values))):
                        return {
                            'type': 'prefix_count',
                            'source': in_col,
                            'condition': condition_type,
                            'a': a,
                            'b': b,
                            'output_column': out_col,
                        }

        return None

    def _check_condition(self, value: Any, condition_type: str) -> bool:
        """Check if value meets the condition."""
        if condition_type == 'not_null':
            return value is not None
        elif condition_type == 'positive':
            return isinstance(value, (int, float)) and value > 0
        elif condition_type == 'negative':
            return isinstance(value, (int, float)) and value < 0
        elif condition_type == 'true':
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
        """Try a specific window operation."""
        window_values = []

        for i in range(len(in_values)):
            start = max(0, i - window_size + 1)
            window = in_values[start:i+1]

            if op == 'sum':
                window_values.append(sum(window))
            elif op == 'mean':
                window_values.append(sum(window) / len(window))

        # Check for linear relationship: out = a * window_values + b
        if len(out_values) >= 2:
            y1, y2 = out_values[0], out_values[1]
            w1, w2 = window_values[0], window_values[1]

            if abs(w2 - w1) < 1e-9:
                if all(abs(out_values[i] - y1) < 1e-9 for i in range(len(out_values))):
                    # Constant
                    return None
                return None

            a = (y2 - y1) / (w2 - w1)
            b = y1 - a * w1

            if all(abs(a * window_values[i] + b - out_values[i]) < 1e-6 for i in range(len(out_values))):
                return {
                    'type': 'sliding_window',
                    'source': in_col,
                    'window_size': window_size,
                    'operation': op,
                    'a': a,
                    'b': b,
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
            val_str = self._py_val_str(val)
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
                val_str = self._py_val_str(val)
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

    @staticmethod
    def _py_val_str(val: Any) -> str:
        if isinstance(val, bool):
            return 'True' if val else 'False'
        if isinstance(val, str):
            return "'" + val + "'"
        return str(val)

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
                lines.append("        result['" + out_col + "'] = " + self._py_val_str(transform['value']))
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

        # Generate state initialization
        if has_stateful:
            lines.extend(self._generate_js_state_init())

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
                    val = self._js_val_str(nf.get('value'))
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

    def _generate_js_state_init(self) -> List[str]:
        """Generate JavaScript state initialization code."""
        return []

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

    @staticmethod
    def _js_val_str(val: Any) -> str:
        if isinstance(val, bool):
            return 'true' if val else 'false'
        if isinstance(val, str):
            return "'" + val + "'"
        return str(val)

    def _generate_filter_logic_js(self) -> List[str]:
        conditions = self.config.get('filter_conditions', [])
        lines = []
        for cond in conditions:
            col = cond['column']
            op = cond['operator']
            val_str = self._js_val_str(cond['value'])
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
                lines.append("    result['" + out_col + "'] = " + self._js_val_str(transform['value']) + ";")
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
                val = self._js_val_str(nf.get('value'))
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
                val = self._js_val_str(nf.get('value'))
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

    args = parser.parse_args()
    if args.python == args.javascript:
        parser.error("Exactly one of --python or --javascript must be specified")

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


if __name__ == '__main__':
    main()
