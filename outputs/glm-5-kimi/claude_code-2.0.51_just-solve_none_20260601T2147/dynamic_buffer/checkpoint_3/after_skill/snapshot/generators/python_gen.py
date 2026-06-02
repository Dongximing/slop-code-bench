#!/usr/bin/env python3
"""Python code generator for streaming data processor modules."""

from typing import Dict, List

from base_generator import CodeGenerator, val_str


class PythonCodeGenerator(CodeGenerator):
    """Generates Python module code from transformation config."""

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
        delimiter = self.delimiter
        has_stateful = self.has_stateful
        has_neighbor_filters = self.has_neighbor_filters

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

        if has_stateful:
            lines.extend(self._generate_state_methods())

        lines.extend([
            '',
            '    def _should_keep_row(self, row: Dict[str, Any]) -> bool:',
        ])
        lines.extend(self._generate_filter_logic())

        if has_neighbor_filters:
            lines.extend(self._generate_neighbor_filter_methods())

        lines.extend([
            '',
            '    def _transform_row(self, row: Dict[str, Any]) -> Dict[str, Any]:',
        ])

        lines.append('        result = {}')
        lines.extend(self._generate_transform_logic())

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
        for i, st in enumerate(self.get_stateful_transforms()):
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
        stateful_transforms = self.get_stateful_transforms()

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
        conditions = self.get_filter_conditions()
        if not conditions:
            return ['        return True']

        lines = []
        for cond in conditions:
            col = cond['column']
            op = cond['operator']
            val = cond['value']
            v = val_str(val, 'py')
            if op == '!=':
                if isinstance(val, bool):
                    lines.append("        if row.get('" + col + "') is " + v + ":")
                else:
                    lines.append("        if row.get('" + col + "') == " + v + ":")
                lines.append("            return False")
            elif op == '==':
                lines.append("        if row.get('" + col + "') != " + v + ":")
                lines.append("            return False")
            elif op in ('>', '>=', '<', '<='):
                comp_ops = {'>': '<=', '>=': '<', '<': '>=', '<=': '>'}
                lines.append("        val = row.get('" + col + "')")
                lines.append("        if val is not None and isinstance(val, (int, float)) and val " + comp_ops[op] + " " + v + ":")
                lines.append("            return False")
        lines.append("        return True")
        return lines

    def _generate_neighbor_filter_methods(self) -> List[str]:
        """Generate methods for neighbor-based filtering."""
        lines = []
        for i, nf in enumerate(self.get_neighbor_filters()):
            nf_type = nf.get('type')

            if nf_type == 'next_row_condition':
                col = nf.get('column')
                val = nf.get('value')
                v = val_str(val, 'py')
                lines.extend([
                    '',
                    '    def _check_neighbor_filter_' + str(i) + '(self, current_row: Dict, next_row: Optional[Dict]) -> bool:',
                    '        """Check if current row should be dropped based on next row."""',
                    '        if next_row is None:',
                    '            return True  # Keep if no next row',
                    "        if next_row.get('" + col + "') == " + v + ":",
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

    def _generate_transform_logic(self) -> List[str]:
        transforms = self.get_column_transforms()
        stateful_output_cols = self.get_stateful_output_columns()
        output_cols = self.get_output_columns()
        lines = []

        for out_col in output_cols:
            if out_col in stateful_output_cols:
                continue

            transform = transforms.get(out_col, {'type': 'unknown'})
            t_type = transform.get('type')
            source = transform.get('source', out_col)
            if t_type in ('identity', 'copy'):
                lines.append("        result['" + out_col + "'] = row.get('" + source + "')")
            elif t_type == 'constant':
                lines.append("        result['" + out_col + "'] = " + val_str(transform['value'], 'py'))
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
        lines = []

        for i, st in enumerate(self.get_stateful_transforms()):
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
            neighbor_filters = self.get_neighbor_filters()
            lines.extend(self._generate_neighbor_filter_logic(neighbor_filters))
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
            '            pending_row = row',
            '            pending_input_idx = row_count',
            '            prev_row = row',
            '            row_count += 1',
        ])

        return lines
