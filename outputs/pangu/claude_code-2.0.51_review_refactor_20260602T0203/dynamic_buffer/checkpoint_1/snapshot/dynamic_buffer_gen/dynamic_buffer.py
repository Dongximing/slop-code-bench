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
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
import csv


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

    # Infer filter conditions
    filter_conditions = _infer_filters(input_rows, output_rows, kept_indices)

    # Infer column transformations
    column_transforms = _infer_column_transforms(
        input_rows, output_rows, kept_indices, input_cols_list, output_cols_list
    )

    return InferredTransformation(
        column_transforms=column_transforms,
        filter_conditions=filter_conditions,
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


def _infer_filters(input_rows: List[Dict], output_rows: List[Dict], kept_indices: List[int]) -> List[FilterCondition]:
    """Infer filter conditions that explain why rows were kept/dropped."""
    filters = []

    if len(input_rows) == len(output_rows):
        return filters  # No filtering

    # For dropped rows, look for column values that explain the drop
    kept_set = set(kept_indices)
    all_cols = set()
    for row in input_rows + output_rows:
        all_cols.update(row.keys())

    for col in all_cols:
        # Look for patterns in kept vs dropped rows
        kept_values = set()
        dropped_values = set()

        for i, row in enumerate(input_rows):
            if col in row:
                if i in kept_set:
                    kept_values.add(row[col])
                else:
                    dropped_values.add(row[col])

        if dropped_values and len(dropped_values) < len(kept_values):
            # Simple filter: column must NOT equal a dropped value
            # Or: column must equal a kept value
            if len(dropped_values) == 1:
                # Likely: col != dropped_value
                val = next(iter(dropped_values))
                filters.append(FilterCondition(
                    column=col,
                    operator='ne',
                    value=val
                ))

    return filters


def _infer_column_transforms(
    input_rows: List[Dict],
    output_rows: List[Dict],
    kept_indices: List[int],
    input_cols: List[str],
    output_cols: List[str]
) -> List[ColumnTransform]:
    """Infer how each output column is derived from input columns."""
    transforms = []

    # Track which columns are used in output
    used_input_cols = set()

    for out_col in output_cols:
        best_transform = None
        best_score = 0

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

        if best_transform:
            transforms.append(best_transform)

    # Add drop transformations for unused input columns
    for in_col in input_cols:
        if in_col not in used_input_cols and in_col not in output_cols:
            transforms.append(ColumnTransform(
                input_column=in_col,
                output_column=in_col,  # Use input name for drop
                transform_type='drop',
                transform_params={}
            ))

    return transforms


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
    """Generate the Python DynamicPreprocessor implementation."""

    filter_code = _generate_python_filter(transform.filter_conditions)
    transform_code, row_transform_func = _generate_python_row_transform(transform)
    reader_setup = _generate_python_reader_setup(transform.file_format)

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

        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def __call__(self, path: str) -> Iterator[Dict[str, Any]]:
        """Process a data file and yield transformed rows."""
        return self._process_file(path)

    def _process_file(self, path: str) -> Iterator[Dict[str, Any]]:
        """Process a file with streaming and optional caching."""
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

    transform_code = '\n'.join(lines)
    return transform_code, transform_code


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
