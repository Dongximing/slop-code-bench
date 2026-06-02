#!/usr/bin/env python3
"""
Dynamic Buffer - A code generator that infers transformations from sample data
and generates modules for streaming data processing.
"""

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


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
        }

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
            return in_val.strip() == out_val or in_val.lower() == out_val or in_val.upper() == out_val or in_val.strip().lower() == out_val or in_val.strip().upper() == out_val
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
        dropped_rows = [self.input_rows[i] for i in dropped_input_indices]
        kept_rows = [self.input_rows[i] for i in kept_input_indices]
        self.filter_conditions = self._find_distinguishing_conditions(kept_rows, dropped_rows)

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
        for out_col in self.output_cols:
            self.column_transforms[out_col] = self._infer_single_column_transform(out_col)

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
            items = [' ' * (indent + 8) + PythonCodeGenerator._py_repr(item, indent + 8) for item in obj]
            return '[\n' + ',\n'.join(items) + '\n' + ' ' * indent + ']'
        if isinstance(obj, dict):
            if not obj:
                return '{}'
            items = [' ' * (indent + 8) + repr(k) + ': ' + PythonCodeGenerator._py_repr(v, indent + 8) for k, v in obj.items()]
            return '{\n' + ',\n'.join(items) + '\n' + ' ' * indent + '}'
        return repr(obj)

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
        lines = [
            '"""',
            'Dynamic Preprocessor - Streaming data processor with caching support.',
            '"""',
            '',
            'import hashlib',
            'import json',
            'import os',
            'from pathlib import Path',
            'from typing import Any, Dict, Iterator, Optional',
            '',
            '',
            'class DynamicPreprocessor:',
            '    """Streaming preprocessor with caching and resuming support."""',
            '',
            '    def __init__(self, buffer: int, cache_dir: Optional[str] = None):',
            '        self.buffer = buffer',
            '        self.cache_dir = cache_dir',
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
            '            return {"processed_rows": 0, "rows": []}',
            "        with open(cache_path, 'r', encoding='utf-8') as f:",
            '            return json.load(f)',
            '',
            '    def _save_cache(self, cache_path: Optional[Path], state: Dict):',
            '        if cache_path is None:',
            '            return',
            "        with open(cache_path, 'w', encoding='utf-8') as f:",
            '            json.dump(state, f)',
        ]

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

        lines.extend([
            '',
            '    def _should_keep_row(self, row: Dict[str, Any]) -> bool:',
        ])
        lines.extend(self._generate_filter_logic())
        lines.extend([
            '',
            '    def _transform_row(self, row: Dict[str, Any]) -> Dict[str, Any]:',
        ])

        # Optimize: if all transforms are identity/copy on same column, emit a single return
        transforms = self.config.get('column_transforms', {})
        output_cols = self.config.get('output_columns', [])
        all_identity = all(
            transforms.get(c, {}).get('type') in ('identity', 'copy')
            for c in output_cols
        )
        if all_identity:
            pairs = [(c, transforms.get(c, {}).get('source', c)) for c in output_cols]
            lines.append("        return {" + ", ".join(f"'{c}': row.get('{s}')" for c, s in pairs) + "}")
        else:
            lines.append('        result = {}')
            lines.extend(self._generate_transform_logic())
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

        lines.extend([
            '',
            '    def __call__(self, path: str) -> Iterator[Dict[str, Any]]:',
            '        cache_path = self._get_cache_path(path)',
            '        cache_state = self._load_cache(cache_path)',
            '        start_row = cache_state.get("processed_rows", 0)',
            '        row_count = 0',
            '        buffer_count = 0',
            '        current_cache_rows = []',
            '',
            '        for row in self._parse_file(path):',
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
            '',
            '        if cache_path is not None and (buffer_count > 0 or row_count > start_row):',
            '            cache_state["processed_rows"] = row_count',
            '            cache_state["rows"] = current_cache_rows',
            '            self._save_cache(cache_path, cache_state)',
            '',
        ])
        return '\n'.join(lines)

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

    @staticmethod
    def _py_val_str(val: Any) -> str:
        if isinstance(val, bool):
            return 'True' if val else 'False'
        if isinstance(val, str):
            return "'" + val + "'"
        return str(val)

    def _generate_transform_logic(self) -> List[str]:
        transforms = self.config.get('column_transforms', {})
        output_cols = self.config.get('output_columns', [])
        lines = []
        for out_col in output_cols:
            transform = transforms.get(out_col, {'type': 'unknown'})
            t_type = transform.get('type')
            source = transform.get('source', out_col)
            if t_type in ('identity', 'copy'):
                lines.append("        result['" + out_col + "'] = row.get('" + source + "')")
            elif t_type == 'constant':
                lines.append("        result['" + out_col + "'] = " + self._py_val_str(transform['value']))
            elif t_type in ('strip', 'lower', 'upper', 'strip_lower', 'strip_upper'):
                method_map = {'strip': 'strip', 'lower': 'lower', 'upper': 'upper', 'strip_lower': 'strip().lower', 'strip_upper': 'strip().upper'}
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
        delimiter = ',' if self.file_ext == 'csv' else '\t' if self.file_ext == 'tsv' else None
        lines = [
            '/**',
            ' * Dynamic Preprocessor - Streaming data processor with caching support.',
            ' */',
            '',
            "const fs = require('fs');",
            "const path = require('path');",
            "const crypto = require('crypto');",
            '',
            'const CONFIG = ' + json.dumps(self.config, indent=2) + ';',
            '',
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
        ]
        lines.extend(self._generate_filter_logic_js())
        lines.extend([
            '    return true;',
            '}',
            '',
            'function transformRow(row) {',
            '    const result = {};',
        ])
        lines.extend(self._generate_transform_logic_js())
        lines.extend([
            '    return result;',
            '}',
            '',
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
            'function loadCache(cachePath) {',
            '    if (!cachePath || !fs.existsSync(cachePath)) {',
            '        return { processedRows: 0, rows: [] };',
            '    }',
            "    return JSON.parse(fs.readFileSync(cachePath, 'utf-8'));",
            '}',
            '',
            'function saveCache(cachePath, state) {',
            '    if (!cachePath) return;',
            '    fs.writeFileSync(cachePath, JSON.stringify(state));',
            '}',
            '',
            'class DynamicPreprocessor {',
            '    constructor({ buffer, cache_dir = null }) {',
            '        this.buffer = buffer;',
            '        this.cacheDir = cache_dir;',
            '    }',
            '',
            '    process(filePath) {',
            '        const self = this;',
            '        return {',
            '            [Symbol.iterator]: function* () {',
            '                const cachePath = getCachePath(filePath, self.cacheDir);',
            '                const cacheState = loadCache(cachePath);',
            '                const startRow = cacheState.processedRows || 0;',
            '',
            '                let rowCount = 0;',
            '                let bufferCount = 0;',
            '                const currentCacheRows = [];',
            '',
            '                for (const row of parseFile(filePath)) {',
            '                    if (rowCount < startRow) {',
            '                        rowCount++;',
            '                        continue;',
            '                    }',
            '',
            '                    if (!shouldKeepRow(row)) {',
            '                        rowCount++;',
            '                        cacheState.processedRows = rowCount;',
            '                        saveCache(cachePath, cacheState);',
            '                        continue;',
            '                    }',
            '',
            '                    const transformed = transformRow(row);',
            '',
            '                    if (cachePath) {',
            '                        currentCacheRows.push(transformed);',
            '                        bufferCount++;',
            '',
            '                        if (bufferCount >= self.buffer) {',
            '                            cacheState.processedRows = rowCount + 1;',
            '                            cacheState.rows = currentCacheRows;',
            '                            saveCache(cachePath, cacheState);',
            '                            currentCacheRows.length = 0;',
            '                            bufferCount = 0;',
            '                        }',
            '                    }',
            '',
            '                    rowCount++;',
            '                    yield transformed;',
            '                }',
            '',
            '                if (cachePath && (bufferCount > 0 || rowCount > startRow)) {',
            '                    cacheState.processedRows = rowCount;',
            '                    cacheState.rows = currentCacheRows;',
            '                    saveCache(cachePath, cacheState);',
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
        output_cols = self.config.get('output_columns', [])
        lines = []
        for out_col in output_cols:
            transform = transforms.get(out_col, {'type': 'unknown'})
            t_type = transform.get('type')
            source = transform.get('source', out_col)
            v = "val_" + out_col
            if t_type in ('identity', 'copy'):
                lines.append("    result['" + out_col + "'] = row['" + source + "'];")
            elif t_type == 'constant':
                lines.append("    result['" + out_col + "'] = " + self._js_val_str(transform['value']) + ";")
            elif t_type in ('strip', 'lower', 'upper', 'strip_lower', 'strip_upper'):
                method_map = {'strip': 'trim', 'lower': 'toLowerCase', 'upper': 'toUpperCase', 'strip_lower': 'trim().toLowerCase', 'strip_upper': 'trim().toUpperCase'}
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
            else:
                lines.append("    result['" + out_col + "'] = row['" + out_col + "'];")
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
