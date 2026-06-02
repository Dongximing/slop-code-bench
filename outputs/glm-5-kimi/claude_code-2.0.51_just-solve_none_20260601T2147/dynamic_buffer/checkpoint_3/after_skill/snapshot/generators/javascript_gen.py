#!/usr/bin/env python3
"""JavaScript code generator for streaming data processor modules."""

import json
from typing import Dict, List

from base_generator import CodeGenerator, val_str


class JavaScriptCodeGenerator(CodeGenerator):
    """Generates JavaScript module code from transformation config."""

    def generate(self) -> str:
        has_stateful = self.has_stateful
        has_neighbor_filters = self.has_neighbor_filters

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
        lines.extend(self._generate_filter_logic())
        lines.extend([
            '    return true;',
            '}',
            '',
            'function transformRow(row, state) {',
            '    const result = {};',
        ])
        lines.extend(self._generate_transform_logic())

        if has_stateful:
            lines.extend(self._generate_stateful_transform())

        lines.extend([
            '    return result;',
            '}',
            '',
        ])

        if has_stateful:
            lines.extend(self._generate_state_methods())

        lines.extend([
            'function* parseFile(filePath) {',
        ])

        if self.file_ext in ('csv', 'tsv'):
            delimiter = self.delimiter
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

        if has_stateful:
            for i, st in enumerate(self.get_stateful_transforms()):
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
            lines.extend(self._generate_neighbor_filter_logic())
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

        if has_neighbor_filters:
            lines.extend([
                '                // Handle pending row at end',
                '                if (pendingRow !== null) {',
                '                    let keep = true;',
            ])

            for i, nf in enumerate(self.get_neighbor_filters()):
                nf_type = nf.get('type')
                if nf_type == 'next_row_condition':
                    col = nf.get('column')
                    v = val_str(nf.get('value'), 'js')
                    lines.extend([
                        f'                    keep = keep && (null === null || pendingRow.next_{col} !== {v});',
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

    def _generate_state_methods(self) -> List[str]:
        """Generate JavaScript state save/restore methods."""
        lines = [
            'function getCurrentState(state) {',
            '    return {',
        ]

        for i, st in enumerate(self.get_stateful_transforms()):
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

        for i, st in enumerate(self.get_stateful_transforms()):
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

    def _generate_filter_logic(self) -> List[str]:
        lines = []
        for cond in self.get_filter_conditions():
            col = cond['column']
            op = cond['operator']
            v = val_str(cond['value'], 'js')
            if op == '!=':
                lines.append("    if (row['" + col + "'] === " + v + ") return false;")
            elif op == '==':
                lines.append("    if (row['" + col + "'] !== " + v + ") return false;")
            elif op in ('>', '>=', '<', '<='):
                comp_ops = {'>': '<=', '>=': '<', '<': '>=', '<=': '>'}
                lines.append("    if (typeof row['" + col + "'] === 'number' && row['" + col + "'] " + comp_ops[op] + " " + v + ") return false;")
        return lines

    def _generate_transform_logic(self) -> List[str]:
        stateful_output_cols = self.get_stateful_output_columns()
        output_cols = self.get_output_columns()
        lines = []
        for out_col in output_cols:
            if out_col in stateful_output_cols:
                continue

            transform = self.get_column_transforms().get(out_col, {'type': 'unknown'})
            t_type = transform.get('type')
            source = transform.get('source', out_col)
            v = "val_" + out_col.replace('-', '_')
            if t_type in ('identity', 'copy'):
                lines.append("    result['" + out_col + "'] = row['" + source + "'];")
            elif t_type == 'constant':
                lines.append("    result['" + out_col + "'] = " + val_str(transform['value'], 'js') + ";")
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

    def _generate_stateful_transform(self) -> List[str]:
        """Generate JavaScript code for stateful transforms."""
        lines = []

        for i, st in enumerate(self.get_stateful_transforms()):
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

    def _generate_neighbor_filter_logic(self) -> List[str]:
        """Generate JavaScript logic for neighbor-based filtering."""
        neighbor_filters = self.get_neighbor_filters()

        lines = [
            '                    if (rowCount < startRow) {',
            '                        if (pendingRow !== null && pendingInputIdx === rowCount) {',
            '                            let keep = true;',
        ]

        for i, nf in enumerate(neighbor_filters):
            nf_type = nf.get('type')
            if nf_type == 'next_row_condition':
                col = nf.get('column')
                v = val_str(nf.get('value'), 'js')
                lines.append(f'                            keep = keep && (row["{col}"] !== {v});')
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
                v = val_str(nf.get('value'), 'js')
                lines.append(f'                        keep = keep && (row["{col}"] !== {v});')
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
