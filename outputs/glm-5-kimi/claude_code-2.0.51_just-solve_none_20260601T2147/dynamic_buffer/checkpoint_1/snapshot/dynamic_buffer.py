#!/usr/bin/env python3
"""
Dynamic Buffer - A code generator that infers transformations from sample data
and generates modules for streaming data processing.
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
import hashlib
import re


# =============================================================================
# Data Parsing
# =============================================================================

def parse_csv(filepath: str, delimiter: str = ',') -> List[Dict[str, Any]]:
    """Parse a CSV/TSV file and return list of row dicts."""
    rows = []
    with open(filepath, 'r', encoding='utf-8') as f:
        lines = f.read().strip().split('\n')

    if not lines:
        return []

    headers = lines[0].split(delimiter)

    for line in lines[1:]:
        if not line.strip():
            continue
        values = line.split(delimiter)
        row = {}
        for i, header in enumerate(headers):
            if i < len(values):
                row[header] = parse_value(values[i])
            else:
                row[header] = None
        rows.append(row)

    return rows


def parse_tsv(filepath: str) -> List[Dict[str, Any]]:
    """Parse a TSV file and return list of row dicts."""
    return parse_csv(filepath, delimiter='\t')


def parse_jsonl(filepath: str) -> List[Dict[str, Any]]:
    """Parse a JSONL file and return list of row dicts."""
    rows = []
    with open(filepath, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def parse_json_file(filepath: str) -> List[Dict[str, Any]]:
    """Parse a JSON file and return list of row dicts."""
    with open(filepath, 'r', encoding='utf-8') as f:
        return json.load(f)


def parse_value(value: str) -> Any:
    """Parse a string value to appropriate Python type."""
    if value == '':
        return None

    # Try boolean
    if value.lower() == 'true':
        return True
    if value.lower() == 'false':
        return False

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


def get_file_extension(filepath: str) -> str:
    """Get file extension without dot."""
    return Path(filepath).suffix.lstrip('.').lower()


def parse_file(filepath: str) -> Tuple[List[Dict[str, Any]], str]:
    """Parse a data file and return rows and extension."""
    ext = get_file_extension(filepath)

    if ext == 'csv':
        return parse_csv(filepath), ext
    elif ext == 'tsv':
        return parse_tsv(filepath), ext
    elif ext == 'jsonl':
        return parse_jsonl(filepath), ext
    elif ext == 'json':
        return parse_json_file(filepath), ext
    else:
        raise ValueError(f"Unsupported file extension: {ext}")


# =============================================================================
# Transformation Inference
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

    def infer(self) -> Dict[str, Any]:
        """Infer all transformations and return a config dict."""
        self._infer_row_mapping()
        self._infer_filter_conditions()
        self._infer_column_transforms()

        return {
            'column_transforms': self.column_transforms,
            'filter_conditions': self.filter_conditions,
            'input_columns': self.input_cols,
            'output_columns': self.output_cols,
            'row_mapping': self.row_mapping,
        }

    def _infer_row_mapping(self):
        """Map output rows to corresponding input rows."""
        used_input_indices = set()
        for out_idx, out_row in enumerate(self.output_rows):
            best_score = -1
            best_in_idx = None
            for in_idx in range(len(self.input_rows)):
                if in_idx in used_input_indices:
                    continue
                in_row = self.input_rows[in_idx]
                score = self._row_match_score(in_row, out_row)
                if score > best_score:
                    best_score = score
                    best_in_idx = in_idx
            if best_in_idx is not None and best_score > 0:
                self.row_mapping[out_idx] = best_in_idx
                used_input_indices.add(best_in_idx)

    def _row_match_score(self, input_row: Dict[str, Any], output_row: Dict[str, Any]) -> int:
        """Score how well an input row matches an output row. Higher is better."""
        score = 0
        for out_col, out_val in output_row.items():
            if out_col in input_row:
                if input_row[out_col] == out_val:
                    score += 10  # Exact match
                elif self._check_transform_match(input_row, out_col, out_val):
                    score += 3  # Potential transform
                elif isinstance(input_row[out_col], (int, float)) and isinstance(out_val, (int, float)):
                    score += 1  # Same type (numeric), could be transform
                else:
                    score -= 5  # Different value, no obvious transform
            else:
                # Output column not in input - check if value exists in other input columns
                found = False
                for in_col, in_val in input_row.items():
                    if in_val == out_val:
                        found = True
                        break
                if found:
                    score += 2  # Value found in another column
                else:
                    score -= 2  # Value not found
        return score

    def _check_transform_match(self, input_row: Dict[str, Any], col: str, out_val: Any) -> bool:
        """Check if output value matches any transform of input value."""
        in_val = input_row.get(col)
        if in_val is None:
            return False

        if isinstance(in_val, str) and isinstance(out_val, str):
            if in_val.strip() == out_val:
                return True
            if in_val.lower() == out_val:
                return True
            if in_val.upper() == out_val:
                return True
            if in_val.strip().lower() == out_val:
                return True
            if in_val.strip().upper() == out_val:
                return True

        # For numeric values, only match if they're very close (possible float/int conversion)
        # Don't match arbitrary different numbers as transforms
        if isinstance(in_val, (int, float)) and isinstance(out_val, (int, float)):
            # Allow for small floating point differences (int to float conversion)
            return abs(in_val - out_val) < 1e-9

        return False

    def _infer_filter_conditions(self):
        """Infer filter conditions that explain which rows were dropped."""
        if not self.output_rows:
            return

        kept_input_indices = set(self.row_mapping.values())
        dropped_input_indices = set(range(len(self.input_rows))) - kept_input_indices

        if not dropped_input_indices:
            return

        dropped_rows = [self.input_rows[i] for i in dropped_input_indices]
        kept_rows = [self.input_rows[i] for i in kept_input_indices]

        conditions = self._find_distinguishing_conditions(kept_rows, dropped_rows)
        self.filter_conditions = conditions

    def _find_distinguishing_conditions(self, kept_rows: List[Dict], dropped_rows: List[Dict]) -> List[Dict]:
        """Find conditions that distinguish kept from dropped rows."""
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
        """Check if equality conditions explain the filter."""
        conditions = []

        unique_dropped = set(str(v) for v in dropped_values if v is not None)
        unique_kept = set(str(v) for v in kept_values if v is not None)

        if len(unique_dropped) == 1:
            val = list(unique_dropped)[0]
            if val not in unique_kept:
                conditions.append({
                    'type': 'equality',
                    'column': col,
                    'operator': '!=',
                    'value': self._parse_filter_value(val)
                })

        return conditions

    def _check_comparison_filter(self, col: str, kept_values: List, dropped_values: List) -> List[Dict]:
        """Check if comparison conditions explain the filter."""
        conditions = []

        try:
            dropped_numeric = [float(v) for v in dropped_values if v is not None]
            kept_numeric = [float(v) for v in kept_values if v is not None]

            if dropped_numeric and kept_numeric:
                max_dropped = max(dropped_numeric)
                min_kept = min(kept_numeric)

                if max_dropped < min_kept:
                    threshold = (max_dropped + min_kept) / 2
                    conditions.append({
                        'type': 'comparison',
                        'column': col,
                        'operator': '>',
                        'value': threshold
                    })

                min_dropped = min(dropped_numeric)
                max_kept = max(kept_numeric)

                if min_dropped < min_kept and max_dropped < min_kept:
                    conditions.append({
                        'type': 'comparison',
                        'column': col,
                        'operator': '>=',
                        'value': min_kept
                    })

        except (ValueError, TypeError):
            pass

        return conditions

    def _parse_filter_value(self, val: str) -> Any:
        """Parse a filter value to appropriate type."""
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
        """Infer transformations for each output column."""
        for out_col in self.output_cols:
            transform = self._infer_single_column_transform(out_col)
            self.column_transforms[out_col] = transform

    def _infer_single_column_transform(self, out_col: str) -> Dict[str, Any]:
        """Infer transformation for a single output column."""
        if not self.output_rows:
            return {'type': 'unknown'}

        out_values = [row.get(out_col) for row in self.output_rows]

        # Check if output column matches an input column exactly
        if out_col in self.input_cols:
            matches = self._check_exact_match(out_col, out_values)
            if matches:
                return {'type': 'identity', 'source': out_col}

        # Check for rename (different name, same values)
        for in_col in self.input_cols:
            if self._check_column_copy(in_col, out_values):
                return {'type': 'copy', 'source': in_col}

        # Check for constant value
        if self._check_constant(out_values):
            return {'type': 'constant', 'value': out_values[0]}

        # Check for string transforms - prefer same-named column first
        if out_col in self.input_cols:
            transform = self._check_string_transform(out_col, out_values)
            if transform:
                return transform

        # Then check other columns
        for in_col in self.input_cols:
            if in_col == out_col:
                continue
            transform = self._check_string_transform(in_col, out_values)
            if transform:
                return transform

        # Check for numeric transforms - prefer same-named column first
        if out_col in self.input_cols:
            transform = self._check_numeric_transform(out_col, out_values)
            if transform:
                return transform

        # Then check other columns
        for in_col in self.input_cols:
            if in_col == out_col:
                continue
            transform = self._check_numeric_transform(in_col, out_values)
            if transform:
                return transform

        # Check for combined column transforms
        transform = self._check_combined_transform(out_values)
        if transform:
            return transform

        return {'type': 'unknown'}

    def _check_exact_match(self, col: str, out_values: List) -> bool:
        """Check if output values exactly match input column values."""
        for out_idx, out_val in enumerate(out_values):
            if out_idx not in self.row_mapping:
                return False
            in_idx = self.row_mapping[out_idx]
            in_val = self.input_rows[in_idx].get(col)
            if in_val != out_val:
                return False
        return True

    def _check_column_copy(self, in_col: str, out_values: List) -> bool:
        """Check if output values match input column values (potential rename/copy)."""
        for out_idx, out_val in enumerate(out_values):
            if out_idx not in self.row_mapping:
                return False
            in_idx = self.row_mapping[out_idx]
            in_val = self.input_rows[in_idx].get(in_col)
            if in_val != out_val:
                return False
        return True

    def _check_constant(self, out_values: List) -> bool:
        """Check if all output values are the same constant."""
        if not out_values:
            return False
        first = out_values[0]
        return all(v == first for v in out_values)

    def _check_string_transform(self, in_col: str, out_values: List) -> Optional[Dict]:
        """Check for string transformations on input column."""
        if not out_values or not isinstance(out_values[0], str):
            return None

        in_values = []
        for out_idx, out_val in enumerate(out_values):
            if out_idx not in self.row_mapping:
                return None
            in_idx = self.row_mapping[out_idx]
            in_val = self.input_rows[in_idx].get(in_col)
            if not isinstance(in_val, str):
                return None
            in_values.append(in_val)

        # Check for strip
        if all(in_values[i].strip() == out_values[i] for i in range(len(out_values))):
            if any(in_values[i] != in_values[i].strip() for i in range(len(out_values))):
                return {'type': 'strip', 'source': in_col}

        # Check for lower
        if all(in_values[i].lower() == out_values[i] for i in range(len(out_values))):
            return {'type': 'lower', 'source': in_col}

        # Check for upper
        if all(in_values[i].upper() == out_values[i] for i in range(len(out_values))):
            return {'type': 'upper', 'source': in_col}

        # Check for strip + lower
        if all(in_values[i].strip().lower() == out_values[i] for i in range(len(out_values))):
            return {'type': 'strip_lower', 'source': in_col}

        # Check for strip + upper
        if all(in_values[i].strip().upper() == out_values[i] for i in range(len(out_values))):
            return {'type': 'strip_upper', 'source': in_col}

        # Check for suffix
        first_in = in_values[0]
        first_out = out_values[0]
        if first_out.startswith(first_in):
            suffix = first_out[len(first_in):]
            if all(out_values[i] == in_values[i] + suffix for i in range(len(out_values))):
                return {'type': 'add_suffix', 'source': in_col, 'suffix': suffix}

        # Check for prefix
        if first_out.endswith(first_in):
            prefix = first_out[:-len(first_in)]
            if all(out_values[i] == prefix + in_values[i] for i in range(len(out_values))):
                return {'type': 'add_prefix', 'source': in_col, 'prefix': prefix}

        return None

    def _check_numeric_transform(self, in_col: str, out_values: List) -> Optional[Dict]:
        """Check for numeric linear transformations on input column."""
        if len(out_values) < 2:
            return None

        in_values = []
        for out_idx, out_val in enumerate(out_values):
            if out_idx not in self.row_mapping:
                return None
            in_idx = self.row_mapping[out_idx]
            in_val = self.input_rows[in_idx].get(in_col)
            if not isinstance(in_val, (int, float)) or not isinstance(out_val, (int, float)):
                return None
            in_values.append(in_val)

        x1, x2 = in_values[0], in_values[1]
        y1, y2 = out_values[0], out_values[1]

        if x1 == x2:
            if y1 == y2:
                return {'type': 'constant', 'value': y1}
            return None

        a = (y2 - y1) / (x2 - x1)
        b = y1 - a * x1

        for i in range(len(out_values)):
            expected = a * in_values[i] + b
            if abs(expected - out_values[i]) > 1e-9:
                return None

        return {'type': 'linear', 'source': in_col, 'a': a, 'b': b}

    def _check_combined_transform(self, out_values: List) -> Optional[Dict]:
        """Check for combined column transformations."""
        return None


# =============================================================================
# Python Code Generator
# =============================================================================

class PythonCodeGenerator:
    """Generates Python module code from transformation config."""

    def __init__(self, module_name: str, config: Dict, file_ext: str):
        self.module_name = module_name
        self.config = config
        self.file_ext = file_ext

    @staticmethod
    def _py_repr(obj, indent=0) -> str:
        """Convert a Python object to its repr string with proper indentation."""
        if obj is None:
            return 'None'
        if isinstance(obj, bool):
            return 'True' if obj else 'False'
        if isinstance(obj, (int, float)):
            return str(obj)
        if isinstance(obj, str):
            return repr(obj)
        if isinstance(obj, list):
            if not obj:
                return '[]'
            items = []
            for item in obj:
                items.append(' ' * (indent + 8) + PythonCodeGenerator._py_repr(item, indent + 8))
            return '[\n' + ',\n'.join(items) + '\n' + ' ' * indent + ']'
        if isinstance(obj, dict):
            if not obj:
                return '{}'
            items = []
            for k, v in obj.items():
                items.append(' ' * (indent + 8) + repr(k) + ': ' + PythonCodeGenerator._py_repr(v, indent + 8))
            return '{\n' + ',\n'.join(items) + '\n' + ' ' * indent + '}'
        return repr(obj)

    def generate(self) -> Dict[str, str]:
        """Generate all files for the Python package."""
        return {
            '__init__.py': self._generate_init(),
            'preprocessor.py': self._generate_preprocessor(),
        }

    def _generate_init(self) -> str:
        """Generate __init__.py file."""
        lines = [
            '"""',
            'Generated dynamic preprocessor module.',
            '"""',
            '',
            'from .preprocessor import DynamicPreprocessor',
            '',
            "__all__ = ['DynamicPreprocessor']",
            '',
        ]
        return '\n'.join(lines)

    def _generate_preprocessor(self) -> str:
        """Generate the main preprocessor.py file."""
        delimiter = ',' if self.file_ext == 'csv' else '\t' if self.file_ext == 'tsv' else None

        lines = []
        lines.append('"""')
        lines.append('Dynamic Preprocessor - Streaming data processor with caching support.')
        lines.append('"""')
        lines.append('')
        lines.append('import json')
        lines.append('import os')
        lines.append('from pathlib import Path')
        lines.append('from typing import Any, Dict, Iterator, Optional')
        lines.append('import hashlib')
        lines.append('')
        lines.append('')
        lines.append('class DynamicPreprocessor:')
        lines.append('    """Streaming preprocessor with caching and resuming support."""')
        lines.append('')
        lines.append('    def __init__(self, buffer: int, cache_dir: Optional[str] = None):')
        lines.append('        """')
        lines.append('        Initialize the preprocessor.')
        lines.append('')
        lines.append('        Args:')
        lines.append('            buffer: Maximum number of rows to keep in memory.')
        lines.append('            cache_dir: Optional directory for caching state.')
        lines.append('        """')
        lines.append('        self.buffer = buffer')
        lines.append('        self.cache_dir = cache_dir')
        lines.append('        self._config = self._get_config()')
        lines.append('')
        lines.append('    def _get_config(self) -> Dict[str, Any]:')
        lines.append('        """Return the transformation configuration."""')
        lines.append('        return ' + self._py_repr(self.config, indent=8))
        lines.append('')
        lines.append('    def _get_cache_path(self, input_path: str) -> Path:')
        lines.append('        """Get cache file path for an input file."""')
        lines.append('        if self.cache_dir is None:')
        lines.append('            return None')
        lines.append('        path_hash = hashlib.md5(os.path.abspath(input_path).encode()).hexdigest()')
        lines.append('        cache_dir = Path(self.cache_dir)')
        lines.append('        cache_dir.mkdir(parents=True, exist_ok=True)')
        lines.append('        return cache_dir / f"{path_hash}.json"')
        lines.append('')
        lines.append('    def _load_cache(self, cache_path: Path) -> Dict:')
        lines.append('        """Load cache state from file."""')
        lines.append('        if cache_path is None or not cache_path.exists():')
        lines.append('            return {"processed_rows": 0, "rows": []}')
        lines.append("        with open(cache_path, 'r', encoding='utf-8') as f:")
        lines.append('            return json.load(f)')
        lines.append('')
        lines.append('    def _save_cache(self, cache_path: Path, state: Dict):')
        lines.append('        """Save cache state to file."""')
        lines.append('        if cache_path is None:')
        lines.append('            return')
        lines.append("        with open(cache_path, 'w', encoding='utf-8') as f:")
        lines.append('            json.dump(state, f)')
        lines.append('')
        lines.append('    def _parse_value(self, value: str) -> Any:')
        lines.append('        """Parse a string value to appropriate Python type."""')
        lines.append("        if value == '':")
        lines.append('            return None')
        lines.append("        if value.lower() == 'true':")
        lines.append('            return True')
        lines.append("        if value.lower() == 'false':")
        lines.append('            return False')
        lines.append('        try:')
        lines.append('            return int(value)')
        lines.append('        except ValueError:')
        lines.append('            pass')
        lines.append('        try:')
        lines.append('            return float(value)')
        lines.append('        except ValueError:')
        lines.append('            pass')
        lines.append('        return value')
        lines.append('')
        lines.append('    def _should_keep_row(self, row: Dict[str, Any]) -> bool:')
        lines.append('        """Check if a row passes all filter conditions."""')

        # Filter logic
        filter_lines = self._generate_filter_logic()
        lines.extend(filter_lines)

        lines.append('')
        lines.append('    def _transform_row(self, row: Dict[str, Any]) -> Dict[str, Any]:')
        lines.append('        """Apply transformations to a row."""')
        lines.append('        result = {}')

        # Transform logic
        transform_lines = self._generate_transform_logic()
        lines.extend(transform_lines)

        lines.append('        return result')
        lines.append('')
        lines.append('    def _parse_file(self, path: str) -> Iterator[Dict[str, Any]]:')
        lines.append('        """Parse input file and yield rows."""')

        # File parsing logic
        if self.file_ext in ('csv', 'tsv'):
            lines.append("        with open(path, 'r', encoding='utf-8') as f:")
            lines.append('            lines = f.readlines()')
            lines.append('')
            lines.append('        if not lines:')
            lines.append('            return')
            lines.append('')
            lines.append("        headers = lines[0].strip().split('" + delimiter + "')")
            lines.append('        for line in lines[1:]:')
            lines.append('            line = line.strip()')
            lines.append('            if not line:')
            lines.append('                continue')
            lines.append("            values = line.split('" + delimiter + "')")
            lines.append('            row = {}')
            lines.append('            for i, header in enumerate(headers):')
            lines.append('                if i < len(values):')
            lines.append('                    row[header] = self._parse_value(values[i])')
            lines.append('                else:')
            lines.append('                    row[header] = None')
            lines.append('            yield row')
        elif self.file_ext == 'jsonl':
            lines.append("        with open(path, 'r', encoding='utf-8') as f:")
            lines.append('            for line in f:')
            lines.append('                line = line.strip()')
            lines.append('                if line:')
            lines.append('                    yield json.loads(line)')
        elif self.file_ext == 'json':
            lines.append("        with open(path, 'r', encoding='utf-8') as f:")
            lines.append('            data = json.load(f)')
            lines.append('        for row in data:')
            lines.append('            yield row')

        lines.append('')
        lines.append('    def __call__(self, path: str) -> Iterator[Dict[str, Any]]:')
        lines.append('        """Process a file and yield transformed rows."""')
        lines.append('        cache_path = self._get_cache_path(path)')
        lines.append('        cache_state = self._load_cache(cache_path)')
        lines.append('        start_row = cache_state.get("processed_rows", 0)')
        lines.append('')
        lines.append('        row_count = 0')
        lines.append('        buffer_count = 0')
        lines.append('        current_cache_rows = []')
        lines.append('')
        lines.append('        for row in self._parse_file(path):')
        lines.append('            if row_count < start_row:')
        lines.append('                row_count += 1')
        lines.append('                continue')
        lines.append('')
        lines.append('            if not self._should_keep_row(row):')
        lines.append('                row_count += 1')
        lines.append('                cache_state["processed_rows"] = row_count')
        lines.append('                self._save_cache(cache_path, cache_state)')
        lines.append('                continue')
        lines.append('')
        lines.append('            transformed = self._transform_row(row)')
        lines.append('')
        lines.append('            if cache_path is not None:')
        lines.append('                current_cache_rows.append(transformed)')
        lines.append('                buffer_count += 1')
        lines.append('')
        lines.append('                if buffer_count >= self.buffer:')
        lines.append('                    cache_state["processed_rows"] = row_count + 1')
        lines.append('                    cache_state["rows"] = current_cache_rows')
        lines.append('                    self._save_cache(cache_path, cache_state)')
        lines.append('                    current_cache_rows = []')
        lines.append('                    buffer_count = 0')
        lines.append('')
        lines.append('            row_count += 1')
        lines.append('            yield transformed')
        lines.append('')
        lines.append('        if cache_path is not None and (buffer_count > 0 or row_count > start_row):')
        lines.append('            cache_state["processed_rows"] = row_count')
        lines.append('            cache_state["rows"] = current_cache_rows')
        lines.append('            self._save_cache(cache_path, cache_state)')
        lines.append('')

        return '\n'.join(lines)

    def _generate_filter_logic(self) -> List[str]:
        """Generate filter condition checking code lines."""
        conditions = self.config.get('filter_conditions', [])
        lines = []

        if not conditions:
            lines.append('        return True  # No filtering')
            return lines

        for cond in conditions:
            col = cond['column']
            op = cond['operator']
            val = cond['value']

            val_str = self._py_val_str(val)

            if op == '!=':
                lines.append("        if row.get('" + col + "') == " + val_str + ":")
                lines.append("            return False")
            elif op == '==':
                lines.append("        if row.get('" + col + "') != " + val_str + ":")
                lines.append("            return False")
            elif op == '>':
                lines.append("        val = row.get('" + col + "')")
                lines.append("        if val is not None and isinstance(val, (int, float)) and val <= " + val_str + ":")
                lines.append("            return False")
            elif op == '>=':
                lines.append("        val = row.get('" + col + "')")
                lines.append("        if val is not None and isinstance(val, (int, float)) and val < " + val_str + ":")
                lines.append("            return False")
            elif op == '<':
                lines.append("        val = row.get('" + col + "')")
                lines.append("        if val is not None and isinstance(val, (int, float)) and val >= " + val_str + ":")
                lines.append("            return False")
            elif op == '<=':
                lines.append("        val = row.get('" + col + "')")
                lines.append("        if val is not None and isinstance(val, (int, float)) and val > " + val_str + ":")
                lines.append("            return False")

        lines.append("        return True")
        return lines

    def _py_val_str(self, val: Any) -> str:
        """Format a Python value as a literal string."""
        if isinstance(val, str):
            return "'" + val + "'"
        elif isinstance(val, bool):
            return 'True' if val else 'False'
        else:
            return str(val)

    def _generate_transform_logic(self) -> List[str]:
        """Generate row transformation code lines."""
        transforms = self.config.get('column_transforms', {})
        output_cols = self.config.get('output_columns', [])

        lines = []
        for out_col in output_cols:
            transform = transforms.get(out_col, {'type': 'unknown'})
            t_type = transform.get('type')

            if t_type == 'identity':
                lines.append("        result['" + out_col + "'] = row.get('" + transform['source'] + "')")
            elif t_type == 'copy':
                lines.append("        result['" + out_col + "'] = row.get('" + transform['source'] + "')")
            elif t_type == 'constant':
                val_str = self._py_val_str(transform['value'])
                lines.append("        result['" + out_col + "'] = " + val_str)
            elif t_type == 'strip':
                lines.append("        val = row.get('" + transform['source'] + "')")
                lines.append("        result['" + out_col + "'] = val.strip() if isinstance(val, str) else val")
            elif t_type == 'lower':
                lines.append("        val = row.get('" + transform['source'] + "')")
                lines.append("        result['" + out_col + "'] = val.lower() if isinstance(val, str) else val")
            elif t_type == 'upper':
                lines.append("        val = row.get('" + transform['source'] + "')")
                lines.append("        result['" + out_col + "'] = val.upper() if isinstance(val, str) else val")
            elif t_type == 'strip_lower':
                lines.append("        val = row.get('" + transform['source'] + "')")
                lines.append("        result['" + out_col + "'] = val.strip().lower() if isinstance(val, str) else val")
            elif t_type == 'strip_upper':
                lines.append("        val = row.get('" + transform['source'] + "')")
                lines.append("        result['" + out_col + "'] = val.strip().upper() if isinstance(val, str) else val")
            elif t_type == 'add_prefix':
                lines.append("        val = row.get('" + transform['source'] + "')")
                lines.append("        result['" + out_col + "'] = '" + transform['prefix'] + "' + str(val) if val is not None else None")
            elif t_type == 'add_suffix':
                lines.append("        val = row.get('" + transform['source'] + "')")
                lines.append("        result['" + out_col + "'] = str(val) + '" + transform['suffix'] + "' if val is not None else None")
            elif t_type == 'linear':
                a = transform['a']
                b = transform['b']
                lines.append("        val = row.get('" + transform['source'] + "')")
                lines.append("        if isinstance(val, (int, float)):")
                lines.append("            result['" + out_col + "'] = " + str(a) + " * val + " + str(b))
                lines.append("        else:")
                lines.append("            result['" + out_col + "'] = None")
            else:
                lines.append("        result['" + out_col + "'] = row.get('" + out_col + "')")

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
        """Generate the JavaScript module code."""
        delimiter = ',' if self.file_ext == 'csv' else '\t' if self.file_ext == 'tsv' else None

        lines = []
        lines.append('/**')
        lines.append(' * Dynamic Preprocessor - Streaming data processor with caching support.')
        lines.append(' */')
        lines.append('')
        lines.append("const fs = require('fs');")
        lines.append("const path = require('path');")
        lines.append("const crypto = require('crypto');")
        lines.append('')
        lines.append('const CONFIG = ' + json.dumps(self.config, indent=2) + ';')
        lines.append('')
        lines.append('function parseValue(value) {')
        lines.append("    if (value === '') return null;")
        lines.append("    if (value.toLowerCase() === 'true') return true;")
        lines.append("    if (value.toLowerCase() === 'false') return false;")
        lines.append('    const intVal = parseInt(value, 10);')
        lines.append('    if (!isNaN(intVal) && String(intVal) === value) return intVal;')
        lines.append('    const floatVal = parseFloat(value);')
        lines.append('    if (!isNaN(floatVal)) return floatVal;')
        lines.append('    return value;')
        lines.append('}')
        lines.append('')
        lines.append('function shouldKeepRow(row) {')

        # Filter logic
        filter_lines = self._generate_filter_logic_js()
        lines.extend(filter_lines)
        lines.append('    return true;')
        lines.append('}')
        lines.append('')
        lines.append('function transformRow(row) {')
        lines.append('    const result = {};')

        # Transform logic
        transform_lines = self._generate_transform_logic_js()
        lines.extend(transform_lines)
        lines.append('    return result;')
        lines.append('}')
        lines.append('')
        lines.append('function* parseFile(filePath) {')

        # File parsing logic
        if self.file_ext in ('csv', 'tsv'):
            lines.append("    const content = fs.readFileSync(filePath, 'utf-8');")
            lines.append("    const lines = content.trim().split('\\n');")
            lines.append('    if (lines.length === 0) return;')
            lines.append('')
            lines.append("    const headers = lines[0].split('" + delimiter + "');")
            lines.append('    for (let i = 1; i < lines.length; i++) {')
            lines.append('        const line = lines[i].trim();')
            lines.append('        if (!line) continue;')
            lines.append("        const values = line.split('" + delimiter + "');")
            lines.append('        const row = {};')
            lines.append('        for (let j = 0; j < headers.length; j++) {')
            lines.append('            row[headers[j]] = j < values.length ? parseValue(values[j]) : null;')
            lines.append('        }')
            lines.append('        yield row;')
            lines.append('    }')
        elif self.file_ext == 'jsonl':
            lines.append("    const content = fs.readFileSync(filePath, 'utf-8');")
            lines.append("    const lines = content.trim().split('\\n');")
            lines.append('    for (const line of lines) {')
            lines.append('        if (line.trim()) {')
            lines.append('            yield JSON.parse(line);')
            lines.append('        }')
            lines.append('    }')
        elif self.file_ext == 'json':
            lines.append("    const content = fs.readFileSync(filePath, 'utf-8');")
            lines.append('    const data = JSON.parse(content);')
            lines.append('    for (const row of data) {')
            lines.append('        yield row;')
            lines.append('    }')

        lines.append('}')
        lines.append('')
        lines.append('function getCachePath(inputPath, cacheDir) {')
        lines.append('    if (!cacheDir) return null;')
        lines.append("    const hash = crypto.createHash('md5').update(path.resolve(inputPath)).digest('hex');")
        lines.append('    if (!fs.existsSync(cacheDir)) {')
        lines.append("        fs.mkdirSync(cacheDir, { recursive: true });")
        lines.append('    }')
        lines.append("    return path.join(cacheDir, `${hash}.json`);")
        lines.append('}')
        lines.append('')
        lines.append('function loadCache(cachePath) {')
        lines.append('    if (!cachePath || !fs.existsSync(cachePath)) {')
        lines.append('        return { processedRows: 0, rows: [] };')
        lines.append('    }')
        lines.append("    return JSON.parse(fs.readFileSync(cachePath, 'utf-8'));")
        lines.append('}')
        lines.append('')
        lines.append('function saveCache(cachePath, state) {')
        lines.append('    if (!cachePath) return;')
        lines.append('    fs.writeFileSync(cachePath, JSON.stringify(state));')
        lines.append('}')
        lines.append('')
        lines.append('class DynamicPreprocessor {')
        lines.append('    constructor({ buffer, cache_dir = null }) {')
        lines.append('        this.buffer = buffer;')
        lines.append('        this.cacheDir = cache_dir;')
        lines.append('    }')
        lines.append('')
        lines.append('    process(filePath) {')
        lines.append('        const self = this;')
        lines.append('        return {')
        lines.append('            [Symbol.iterator]: function* () {')
        lines.append('                const cachePath = getCachePath(filePath, self.cacheDir);')
        lines.append('                const cacheState = loadCache(cachePath);')
        lines.append('                const startRow = cacheState.processedRows || 0;')
        lines.append('')
        lines.append('                let rowCount = 0;')
        lines.append('                let bufferCount = 0;')
        lines.append('                const currentCacheRows = [];')
        lines.append('')
        lines.append('                for (const row of parseFile(filePath)) {')
        lines.append('                    if (rowCount < startRow) {')
        lines.append('                        rowCount++;')
        lines.append('                        continue;')
        lines.append('                    }')
        lines.append('')
        lines.append('                    if (!shouldKeepRow(row)) {')
        lines.append('                        rowCount++;')
        lines.append('                        cacheState.processedRows = rowCount;')
        lines.append('                        saveCache(cachePath, cacheState);')
        lines.append('                        continue;')
        lines.append('                    }')
        lines.append('')
        lines.append('                    const transformed = transformRow(row);')
        lines.append('')
        lines.append('                    if (cachePath) {')
        lines.append('                        currentCacheRows.push(transformed);')
        lines.append('                        bufferCount++;')
        lines.append('')
        lines.append('                        if (bufferCount >= self.buffer) {')
        lines.append('                            cacheState.processedRows = rowCount + 1;')
        lines.append('                            cacheState.rows = currentCacheRows;')
        lines.append('                            saveCache(cachePath, cacheState);')
        lines.append('                            currentCacheRows.length = 0;')
        lines.append('                            bufferCount = 0;')
        lines.append('                        }')
        lines.append('                    }')
        lines.append('')
        lines.append('                    rowCount++;')
        lines.append('                    yield transformed;')
        lines.append('                }')
        lines.append('')
        lines.append('                if (cachePath && (bufferCount > 0 || rowCount > startRow)) {')
        lines.append('                    cacheState.processedRows = rowCount;')
        lines.append('                    cacheState.rows = currentCacheRows;')
        lines.append('                    saveCache(cachePath, cacheState);')
        lines.append('                }')
        lines.append('            }')
        lines.append('        };')
        lines.append('    }')
        lines.append('')
        lines.append('    [Symbol.iterator]() {')
        lines.append("        throw new Error('Call process(filePath) to get an iterator');")
        lines.append('    }')
        lines.append('}')
        lines.append('')
        lines.append('function createPreprocessor(options) {')
        lines.append('    const preprocessor = new DynamicPreprocessor(options);')
        lines.append('    return function(filePath) {')
        lines.append('        return preprocessor.process(filePath);')
        lines.append('    };')
        lines.append('}')
        lines.append('')
        lines.append('module.exports = { DynamicPreprocessor, createPreprocessor };')
        lines.append('')

        return '\n'.join(lines)

    def _js_val_str(self, val: Any) -> str:
        """Format a JavaScript value as a literal string."""
        if isinstance(val, str):
            return "'" + val + "'"
        elif isinstance(val, bool):
            return 'true' if val else 'false'
        else:
            return str(val)

    def _generate_filter_logic_js(self) -> List[str]:
        """Generate JavaScript filter condition code lines."""
        conditions = self.config.get('filter_conditions', [])
        lines = []

        for cond in conditions:
            col = cond['column']
            op = cond['operator']
            val = cond['value']
            val_str = self._js_val_str(val)

            if op == '!=':
                lines.append("    if (row['" + col + "'] === " + val_str + ") return false;")
            elif op == '==':
                lines.append("    if (row['" + col + "'] !== " + val_str + ") return false;")
            elif op == '>':
                lines.append("    if (typeof row['" + col + "'] === 'number' && row['" + col + "'] <= " + val_str + ") return false;")
            elif op == '>=':
                lines.append("    if (typeof row['" + col + "'] === 'number' && row['" + col + "'] < " + val_str + ") return false;")
            elif op == '<':
                lines.append("    if (typeof row['" + col + "'] === 'number' && row['" + col + "'] >= " + val_str + ") return false;")
            elif op == '<=':
                lines.append("    if (typeof row['" + col + "'] === 'number' && row['" + col + "'] > " + val_str + ") return false;")

        return lines

    def _generate_transform_logic_js(self) -> List[str]:
        """Generate JavaScript row transformation code lines."""
        transforms = self.config.get('column_transforms', {})
        output_cols = self.config.get('output_columns', [])

        lines = []
        for out_col in output_cols:
            transform = transforms.get(out_col, {'type': 'unknown'})
            t_type = transform.get('type')

            if t_type == 'identity':
                lines.append("    result['" + out_col + "'] = row['" + transform['source'] + "'];")
            elif t_type == 'copy':
                lines.append("    result['" + out_col + "'] = row['" + transform['source'] + "'];")
            elif t_type == 'constant':
                val_str = self._js_val_str(transform['value'])
                lines.append("    result['" + out_col + "'] = " + val_str + ";")
            elif t_type == 'strip':
                lines.append("    const val_" + out_col + " = row['" + transform['source'] + "'];")
                lines.append("    result['" + out_col + "'] = typeof val_" + out_col + " === 'string' ? val_" + out_col + ".trim() : val_" + out_col + ";")
            elif t_type == 'lower':
                lines.append("    const val_" + out_col + " = row['" + transform['source'] + "'];")
                lines.append("    result['" + out_col + "'] = typeof val_" + out_col + " === 'string' ? val_" + out_col + ".toLowerCase() : val_" + out_col + ";")
            elif t_type == 'upper':
                lines.append("    const val_" + out_col + " = row['" + transform['source'] + "'];")
                lines.append("    result['" + out_col + "'] = typeof val_" + out_col + " === 'string' ? val_" + out_col + ".toUpperCase() : val_" + out_col + ";")
            elif t_type == 'strip_lower':
                lines.append("    const val_" + out_col + " = row['" + transform['source'] + "'];")
                lines.append("    result['" + out_col + "'] = typeof val_" + out_col + " === 'string' ? val_" + out_col + ".trim().toLowerCase() : val_" + out_col + ";")
            elif t_type == 'strip_upper':
                lines.append("    const val_" + out_col + " = row['" + transform['source'] + "'];")
                lines.append("    result['" + out_col + "'] = typeof val_" + out_col + " === 'string' ? val_" + out_col + ".trim().toUpperCase() : val_" + out_col + ";")
            elif t_type == 'add_prefix':
                prefix = transform['prefix']
                lines.append("    const val_" + out_col + " = row['" + transform['source'] + "'];")
                lines.append("    result['" + out_col + "'] = val_" + out_col + " != null ? '" + prefix + "' + String(val_" + out_col + ") : null;")
            elif t_type == 'add_suffix':
                suffix = transform['suffix']
                lines.append("    const val_" + out_col + " = row['" + transform['source'] + "'];")
                lines.append("    result['" + out_col + "'] = val_" + out_col + " != null ? String(val_" + out_col + ") + '" + suffix + "' : null;")
            elif t_type == 'linear':
                a = transform['a']
                b = transform['b']
                lines.append("    const val_" + out_col + " = row['" + transform['source'] + "'];")
                lines.append("    result['" + out_col + "'] = typeof val_" + out_col + " === 'number' ? " + str(a) + " * val_" + out_col + " + " + str(b) + " : null;")
            else:
                lines.append("    result['" + out_col + "'] = row['" + out_col + "'];")

        return lines


# =============================================================================
# Main Entry Point
# =============================================================================

def find_sample_files(sample_dir: str) -> Tuple[str, str]:
    """Find input and output files in sample directory."""
    sample_path = Path(sample_dir)

    input_file = None
    output_file = None

    for ext in ['csv', 'tsv', 'jsonl', 'json']:
        potential_input = sample_path / ('input.' + ext)
        potential_output = sample_path / ('output.' + ext)

        if potential_input.exists() and potential_output.exists():
            input_file = str(potential_input)
            output_file = str(potential_output)
            break

    if input_file is None or output_file is None:
        raise ValueError("Could not find matching input/output pair in " + sample_dir)

    return input_file, output_file


def main():
    parser = argparse.ArgumentParser(description='Dynamic Buffer Code Generator')
    parser.add_argument('module_name', help='Name of the generated module')
    parser.add_argument('--output', required=True, help='Output directory')
    parser.add_argument('--sample', required=True, help='Sample directory containing input/output files')
    parser.add_argument('--python', action='store_true', help='Generate Python module')
    parser.add_argument('--javascript', action='store_true', help='Generate JavaScript module')

    args = parser.parse_args()

    # Validate language flags
    if not args.python and not args.javascript:
        parser.error("Exactly one of --python or --javascript must be specified")

    if args.python and args.javascript:
        parser.error("Exactly one of --python or --javascript must be specified")

    # Find sample files
    input_file, output_file = find_sample_files(args.sample)
    file_ext = get_file_extension(input_file)

    # Parse sample files
    input_rows, _ = parse_file(input_file)
    output_rows, _ = parse_file(output_file)

    # Infer transformations
    inferrer = TransformationInferrer(input_rows, output_rows)
    config = inferrer.infer()

    # Generate code
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.python:
        generator = PythonCodeGenerator(args.module_name, config, file_ext)
        files = generator.generate()

        # Create package directory
        package_dir = output_dir / args.module_name
        package_dir.mkdir(parents=True, exist_ok=True)

        for filename, content in files.items():
            filepath = package_dir / filename
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(content)

        print("Generated Python package:", package_dir)

    elif args.javascript:
        generator = JavaScriptCodeGenerator(args.module_name, config, file_ext)
        code = generator.generate()

        # Create module directory
        module_dir = output_dir / args.module_name
        module_dir.mkdir(parents=True, exist_ok=True)

        # Write main module file
        module_file = module_dir / 'index.js'
        with open(module_file, 'w', encoding='utf-8') as f:
            f.write(code)

        print("Generated JavaScript module:", module_file)


if __name__ == '__main__':
    main()
