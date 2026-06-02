#!/usr/bin/env python3
"""Rust code generator for streaming data processor modules.

Supports:
- Simple transforms (identity, string ops, linear)
- Filtering conditions
- Stateful transforms (prefix sum, prefix count, sliding window)
- Advanced window functions (centered windows, median, partitions)
- Ranking functions (row_number, rank, dense_rank)
- Complex state resets (segment-based, multi-column)
"""

from typing import Dict, List

from base_generator import CodeGenerator, val_str


class RustCodeGenerator(CodeGenerator):
    """Generates Rust crate code from transformation config."""

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
        has_stateful = self.has_stateful
        has_neighbor_filters = self.has_neighbor_filters
        has_centered = self.has_centered_windows
        has_partitioned = self.has_partitioned_state
        max_lookahead = self.get_max_lookahead()

        lines = [
            '//! Dynamic Preprocessor - Streaming data processor with caching support.',
            '//! Supports stateful transforms, window functions, partitioned windows, and ranking.',
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
            '    /// Compute median using lower-middle rule for even count.',
            '    fn compute_median(values: &[f64]) -> f64 {',
            '        if values.is_empty() { return 0.0; }',
            '        let mut sorted = values.to_vec();',
            '        sorted.sort_by(|a, b| a.partial_cmp(b).unwrap());',
            '        let k = sorted.len();',
            '        let idx = (k - 1) / 2;',
            '        sorted[idx]',
            '    }',
            '',
        ]

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

        if has_centered:
            lines.extend([
                '        lookahead_buffer: Vec<(Row, usize)>,',
                f'        max_lookahead: usize,',
            ])

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

        lines.extend(self._generate_rust_impl())

        lines.extend([
            '',
            '    impl Iterator for DynamicPreprocessor {',
            '        type Item = Row;',
            '',
            '        fn next(&mut self) -> Option<Self::Item> {',
            '            self.next_row()',
            '        }',
            '    }',
            '',
            '    // Hash function for cache keys',
            '    fn md5_hash(data: &[u8]) -> u128 {',
            '        let mut hash: u128 = 5381;',
            '        for &b in data {',
            '            hash = ((hash << 5).wrapping_add(hash)) ^ b as u128;',
            '        }',
            '        hash',
            '    }',
            '',
            '}  // mod api',
            '',
        ])

        return '\n'.join(lines)

    def _generate_rust_state_fields(self) -> List[str]:
        lines = []
        for i, st in enumerate(self.get_stateful_transforms()):
            st_type = st.get('type')
            if st_type == 'prefix_sum':
                lines.append(f'        prefix_sum_{i}: f64,')
            elif st_type == 'prefix_count':
                lines.append(f'        prefix_count_{i}: i64,')
            elif st_type == 'row_number':
                lines.append(f'        row_number_{i}: i64,')
            elif st_type == 'sliding_window':
                lines.append(f'        window_{i}: Vec<f64>,')
                window_size = st.get('window_size', 3)
                lines.append(f'        window_size_{i}: usize,')
            elif st_type == 'centered_window':
                lines.append(f'        centered_buffer_{i}: Vec<f64>,')
            elif st_type == 'state_machine':
                lines.append(f'        state_{i}: i64,')
            elif st_type in ('partitioned_window', 'partitioned_row_number'):
                lines.append(f'        partition_window_{i}: BTreeMap<String, Vec<f64>>,')
                lines.append(f'        partition_count_{i}: BTreeMap<String, i64>,')
            elif st_type in ('rank', 'dense_rank'):
                lines.append(f'        rank_state_{i}: BTreeMap<String, Vec<f64>>,')
        return lines

    def _generate_rust_impl(self) -> List[str]:
        has_stateful = self.has_stateful
        has_neighbor_filters = self.has_neighbor_filters
        has_centered = self.has_centered_windows
        has_partitioned = self.has_partitioned_state
        max_lookahead = self.get_max_lookahead()

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
            for i, st in enumerate(self.get_stateful_transforms()):
                st_type = st.get('type')
                if st_type == 'prefix_sum':
                    lines.append(f'                prefix_sum_{i}: 0.0,')
                elif st_type == 'prefix_count':
                    lines.append(f'                prefix_count_{i}: 0,')
                elif st_type == 'row_number':
                    lines.append(f'                row_number_{i}: 0,')
                elif st_type == 'sliding_window':
                    window_size = st.get('window_size', 3)
                    lines.append(f'                window_{i}: Vec::new(),')
                    lines.append(f'                window_size_{i}: {window_size},')
                elif st_type == 'centered_window':
                    lines.append(f'                centered_buffer_{i}: Vec::new(),')
                elif st_type == 'state_machine':
                    initial = st.get('initial_state', 1)
                    lines.append(f'                state_{i}: {initial},')
                elif st_type in ('partitioned_window', 'partitioned_row_number'):
                    lines.append(f'                partition_window_{i}: BTreeMap::new(),')
                    lines.append(f'                partition_count_{i}: BTreeMap::new(),')
                elif st_type in ('rank', 'dense_rank'):
                    lines.append(f'                rank_state_{i}: BTreeMap::new(),')

        if has_centered:
            lines.extend([
                '                lookahead_buffer: Vec::new(),',
                f'                max_lookahead: {max_lookahead},',
            ])

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

        lines.extend(self._generate_rust_open_method())
        lines.extend(self._generate_rust_helper_methods())

        if has_partitioned:
            lines.extend(self._generate_rust_partition_key_helper())

        lines.extend(self._generate_rust_should_keep_row())
        lines.extend(self._generate_rust_transform_row())

        if has_neighbor_filters:
            lines.extend(self._generate_rust_neighbor_filter_methods())

        if has_neighbor_filters:
            lines.extend(self._generate_rust_iterator_with_neighbor_filters())
        elif has_centered:
            lines.extend(self._generate_rust_iterator_with_centered_windows())
        else:
            lines.extend(self._generate_rust_iterator_standard())

        lines.extend([
            '    }',
            '',
        ])

        return lines

    def _generate_rust_partition_key_helper(self) -> List[str]:
        """Generate partition key helper method."""
        partition_cols = self.get_partition_columns()
        if not partition_cols:
            return []

        lines = [
            '        fn get_partition_key(row: &BTreeMap<String, String>) -> String {',
            '            let cols: &[&str] = &[' + ', '.join(f'"{c}"' for c in partition_cols) + '];',
            '            let mut key = String::new();',
            '            let mut first = true;',
            '            for col in cols {',
            '                if !first { key.push(\'|\'); }',
            '                first = false;',
            '                if let Some(v) = row.get(*col) {',
            '                    key.push_str(v);',
            '                }',
            '            }',
            '            key',
            '        }',
            '',
        ]
        return lines

    def _generate_rust_open_method(self) -> List[str]:
        has_stateful = self.has_stateful

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
            for i, st in enumerate(self.get_stateful_transforms()):
                st_type = st.get('type')
                if st_type == 'prefix_sum':
                    lines.append(f'            self.prefix_sum_{i} = 0.0;')
                elif st_type == 'prefix_count':
                    lines.append(f'            self.prefix_count_{i} = 0;')
                elif st_type == 'row_number':
                    lines.append(f'            self.row_number_{i} = 0;')
                elif st_type == 'sliding_window':
                    lines.append(f'            self.window_{i}.clear();')
                elif st_type == 'centered_window':
                    lines.append(f'            self.centered_buffer_{i}.clear();')
                elif st_type == 'state_machine':
                    initial = st.get('initial_state', 1)
                    lines.append(f'            self.state_{i} = {initial};')
                elif st_type in ('partitioned_window', 'partitioned_row_number'):
                    lines.append(f'            self.partition_window_{i}.clear();')
                    lines.append(f'            self.partition_count_{i}.clear();')
                elif st_type in ('rank', 'dense_rank'):
                    lines.append(f'            self.rank_state_{i}.clear();')

        lines.extend([
            '',
        ])

        if self.file_ext in ('csv', 'tsv'):
            delim = "','" if self.file_ext == 'csv' else "'\\t'"
            lines.extend([
                '            let file = File::open(&path)?;',
                '            let reader = BufReader::new(file);',
                '            let mut lines = reader.lines();',
                '',
                '            let headers: Vec<String> = if let Some(Ok(line)) = lines.next() {',
                f'                line.split({delim}).map(|s| s.trim().to_string()).collect()',
                '            } else {',
                '                return Ok(());',
                '            };',
                '',
                '            for line in lines {',
                '                let line = line?;',
                '                if line.trim().is_empty() { continue; }',
                f'                let values: Vec<&str> = line.split({delim}).collect();',
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
        has_stateful = self.has_stateful

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
            for i, st in enumerate(self.get_stateful_transforms()):
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
                elif st_type == 'row_number':
                    lines.extend([
                        f'                            if let Some(v) = state.get("row_number_{i}").and_then(|v| v.as_i64()) {{',
                        f'                                self.row_number_{i} = v;',
                        '                            }',
                    ])
                elif st_type == 'sliding_window':
                    lines.extend([
                        f'                            if let Some(v) = state.get("window_{i}").and_then(|v| v.as_array()) {{',
                        f'                                self.window_{i} = v.iter().filter_map(|x| x.as_f64()).collect();',
                        '                            }',
                    ])
                elif st_type == 'centered_window':
                    lines.extend([
                        f'                            if let Some(v) = state.get("centered_buffer_{i}").and_then(|v| v.as_array()) {{',
                        f'                                self.centered_buffer_{i} = v.iter().filter_map(|x| x.as_f64()).collect();',
                        '                            }',
                    ])
                elif st_type == 'state_machine':
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
            for i, st in enumerate(self.get_stateful_transforms()):
                st_type = st.get('type')
                if st_type == 'prefix_sum':
                    lines.append(f'            state_obj.insert("prefix_sum_{i}".to_string(), serde_json::json!(self.prefix_sum_{i}));')
                elif st_type == 'prefix_count':
                    lines.append(f'            state_obj.insert("prefix_count_{i}".to_string(), serde_json::json!(self.prefix_count_{i}));')
                elif st_type == 'row_number':
                    lines.append(f'            state_obj.insert("row_number_{i}".to_string(), serde_json::json!(self.row_number_{i}));')
                elif st_type == 'sliding_window':
                    lines.append(f'            state_obj.insert("window_{i}".to_string(), serde_json::json!(self.window_{i}));')
                elif st_type == 'centered_window':
                    lines.append(f'            state_obj.insert("centered_buffer_{i}".to_string(), serde_json::json!(self.centered_buffer_{i}));')
                elif st_type == 'state_machine':
                    lines.append(f'            state_obj.insert("state_{i}".to_string(), serde_json::json!(self.state_{i}));')
                elif st_type in ('partitioned_window', 'partitioned_row_number'):
                    lines.append(f'            state_obj.insert("partition_window_{i}".to_string(), serde_json::json!({{}}));')
                elif st_type in ('rank', 'dense_rank'):
                    lines.append(f'            state_obj.insert("rank_state_{i}".to_string(), serde_json::json!({{}}));')

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
        conditions = self.get_filter_conditions()

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
                        val_str_val = 'true' if val else 'false'
                        lines.append(f'            if row.get("{col}").map(|v| v.as_str()) == Some("{val_str_val}") {{ return false; }}')
                    else:
                        lines.append(f'            if row.get("{col}").map(|v| v.as_str()) == Some("{val}") {{ return false; }}')
                elif op == '==':
                    if isinstance(val, bool):
                        val_str_val = 'true' if val else 'false'
                        lines.append(f'            if row.get("{col}").map(|v| v.as_str()) != Some("{val_str_val}") {{ return false; }}')
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
        stateful_output_cols = self.get_stateful_output_columns()
        output_cols = self.get_output_columns()
        has_partitioned = self.has_partitioned_state

        lines = [
            '        fn transform_row(&mut self, row: &BTreeMap<String, String>) -> Row {',
            '            let mut result = Row::new();',
            '',
        ]

        for out_col in output_cols:
            if out_col in stateful_output_cols:
                continue

            transform = self.get_column_transforms().get(out_col, {'type': 'unknown'})
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
        for i, st in enumerate(self.get_stateful_transforms()):
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

            elif st_type == 'row_number':
                lines.extend([
                    f'            self.row_number_{i} += 1;',
                    f'            result.insert("{out_col}".to_string(), Value::Int(self.row_number_{i}));',
                ])

            elif st_type == 'sliding_window':
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
                    lines.append(f'            let window_val_{i}: f64 = self.window_{i}.iter().sum();')
                elif operation == 'mean':
                    lines.extend([
                        f'            let window_val_{i} = if self.window_{i}.is_empty() {{ 0.0 }} else {{',
                        f'                self.window_{i}.iter().sum::<f64>() / self.window_{i}.len() as f64',
                        '            };',
                    ])
                elif operation == 'median':
                    lines.append(f'            let window_val_{i} = compute_median(&self.window_{i});')
                lines.append(f'            result.insert("{out_col}".to_string(), Value::Float({a}f64 * window_val_{i} + {b}f64));')

            elif st_type == 'centered_window':
                operation = st.get('operation', 'mean')
                lookbehind = st.get('lookbehind', 0)
                lookahead = st.get('lookahead', 0)
                lines.extend([
                    f'            if let Some(v) = row.get("{source}") {{',
                    f'                if let Ok(n) = v.parse::<f64>() {{',
                    f'                    self.centered_buffer_{i}.push(n);',
                    '                }',
                    '            }',
                    f'            let n_cw = self.centered_buffer_{i}.len();',
                    f'            let start_cw = if n_cw > {lookbehind} {{ n_cw - 1 - {lookbehind} }} else {{ 0 }};',
                    f'            let end_cw = std::cmp::min(n_cw + {lookahead}, n_cw);',
                    f'            let window_cw: Vec<f64> = self.centered_buffer_{i}[start_cw..end_cw].to_vec();',
                ])
                if operation == 'mean':
                    lines.extend([
                        f'            let window_val_{i} = if window_cw.is_empty() {{ 0.0 }} else {{',
                        f'                window_cw.iter().sum::<f64>() / window_cw.len() as f64',
                        '            };',
                    ])
                elif operation == 'median':
                    lines.append(f'            let window_val_{i} = compute_median(&window_cw);')
                elif operation == 'sum':
                    lines.append(f'            let window_val_{i}: f64 = window_cw.iter().sum();')
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

            elif st_type == 'partitioned_window':
                window_size = st.get('window_size', 3)
                operation = st.get('operation', 'mean')
                lines.extend([
                    f'            let pkey_{i} = Self::get_partition_key(row);',
                    f'            if let Some(v) = row.get("{source}") {{',
                    f'                if let Ok(n) = v.parse::<f64>() {{',
                    f'                    self.partition_window_{i}.entry(pkey_{i}.clone()).or_insert_with(Vec::new).push(n);',
                    f'                    if let Some(w) = self.partition_window_{i}.get_mut(&pkey_{i}) {{',
                    f'                        while w.len() > {window_size} {{ w.remove(0); }}',
                    '                    }',
                    '                }',
                    '            }',
                    f'            let pw_{i} = self.partition_window_{i}.get(&pkey_{i}).cloned().unwrap_or_default();',
                ])
                if operation == 'sum':
                    lines.append(f'            let window_val_{i}: f64 = pw_{i}.iter().sum();')
                elif operation == 'mean':
                    lines.extend([
                        f'            let window_val_{i} = if pw_{i}.is_empty() {{ 0.0 }} else {{',
                        f'                pw_{i}.iter().sum::<f64>() / pw_{i}.len() as f64',
                        '            };',
                    ])
                elif operation == 'median':
                    lines.append(f'            let window_val_{i} = compute_median(&pw_{i});')
                lines.append(f'            result.insert("{out_col}".to_string(), Value::Float({a}f64 * window_val_{i} + {b}f64));')

            elif st_type == 'partitioned_row_number':
                lines.extend([
                    f'            let pkey_{i} = Self::get_partition_key(row);',
                    f'            *self.partition_count_{i}.entry(pkey_{i}).or_insert(0) += 1;',
                    f'            let row_num_{i} = self.partition_count_{i}.get(&pkey_{i}).copied().unwrap_or(0);',
                    f'            result.insert("{out_col}".to_string(), Value::Int(row_num_{i}));',
                ])

            elif st_type in ('rank', 'dense_rank'):
                order = st.get('order', 'desc')
                lines.extend([
                    f'            let pkey_{i} = Self::get_partition_key(row);',
                    f'            if let Some(v) = row.get("{source}") {{',
                    f'                if let Ok(n) = v.parse::<f64>() {{',
                    f'                    self.rank_state_{i}.entry(pkey_{i}.clone()).or_insert_with(Vec::new).push(n);',
                    '                }',
                    '            }',
                    f'            let vals_{i} = self.rank_state_{i}.get(&pkey_{i}).cloned().unwrap_or_default();',
                ])
                if order == 'desc':
                    lines.append(f'            let mut sorted_{i} = vals_{i}.clone(); sorted_{i}.sort_by(|a, b| b.partial_cmp(a).unwrap());')
                else:
                    lines.append(f'            let mut sorted_{i} = vals_{i}.clone(); sorted_{i}.sort_by(|a, b| a.partial_cmp(b).unwrap());')

                if st_type == 'rank':
                    lines.extend([
                        f'            let cur_val_{i} = row.get("{source}").and_then(|v| v.parse::<f64>().ok()).unwrap_or(0.0);',
                        f'            let mut rank_{i}: i64 = 1;',
                        f'            for (j, &sv) in sorted_{i}.iter().enumerate() {{',
                        f'                if (sv - cur_val_{i}).abs() < 1e-9 {{',
                        f'                    rank_{i} = (j + 1) as i64;',
                        '                    break;',
                        '                }',
                        '            }',
                        f'            result.insert("{out_col}".to_string(), Value::Int(rank_{i}));',
                    ])
                else:  # dense_rank
                    lines.extend([
                        '            let mut unique_sorted: Vec<f64> = Vec::new();',
                        f'            for sv in &sorted_{i} {{',
                        '                if unique_sorted.last().map(|l| (l - sv).abs() > 1e-9).unwrap_or(true) {{',
                        '                    unique_sorted.push(*sv);',
                        '                }',
                        '            }',
                        f'            let cur_val_{i} = row.get("{source}").and_then(|v| v.parse::<f64>().ok()).unwrap_or(0.0);',
                        f'            let mut rank_{i}: i64 = 1;',
                        '            for (j, &sv) in unique_sorted.iter().enumerate() {',
                        f'                if (sv - cur_val_{i}).abs() < 1e-9 {{',
                        f'                    rank_{i} = (j + 1) as i64;',
                        '                    break;',
                        '                }',
                        '            }',
                        f'            result.insert("{out_col}".to_string(), Value::Int(rank_{i}));',
                    ])

        lines.extend([
            '            result',
            '        }',
            '',
        ])

        return lines

    def _generate_rust_neighbor_filter_methods(self) -> List[str]:
        """Generate methods for neighbor-based filtering."""
        lines = []
        for i, nf in enumerate(self.get_neighbor_filters()):
            nf_type = nf.get('type')

            if nf_type == 'next_row_condition':
                col = nf.get('column')
                val = nf.get('value')
                if isinstance(val, bool):
                    val_str_val = 'true' if val else 'false'
                else:
                    val_str_val = str(val)
                lines.extend([
                    f'        fn check_neighbor_filter_{i}(&self, current_row: &BTreeMap<String, String>, next_row: Option<&BTreeMap<String, String>>) -> bool {{',
                    '            if let Some(next) = next_row {',
                    f'                if next.get("{col}").map(|v| v.as_str()) == Some("{val_str_val}") {{',
                    '                    return false;',
                    '                }',
                    '            }',
                    '            true',
                    '        }',
                    '',
                ])
            elif nf_type == 'consecutive_duplicate':
                col = nf.get('column')
                lines.extend([
                    f'        fn check_neighbor_filter_{i}(&self, current_row: &BTreeMap<String, String>, prev_row: Option<&BTreeMap<String, String>>) -> bool {{',
                    '            if let Some(prev) = prev_row {',
                    f'                if prev.get("{col}").map(|v| v.as_str()) == current_row.get("{col}").map(|v| v.as_str()) {{',
                    '                    return false;',
                    '                }',
                    '            }',
                    '            true',
                    '        }',
                    '',
                ])

        return lines

    def _generate_rust_iterator_standard(self) -> List[str]:
        return [
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

    def _generate_rust_iterator_with_centered_windows(self) -> List[str]:
        max_lookahead = self.get_max_lookahead()
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
            '',
            '                if !self.should_keep_row(&raw_row) {',
            '                    self.processed_rows = self.current_idx;',
            '                    if let Some(ref p) = cache_path { self.save_cache(p); }',
            '                    continue;',
            '                }',
            '',
            '                let transformed = self.transform_row(&raw_row);',
            '                self.lookahead_buffer.push((transformed, self.current_idx));',
            '',
            f'                while self.lookahead_buffer.len() > {max_lookahead} {{',
            '                    let (emitted, emit_idx) = self.lookahead_buffer.remove(0);',
            '',
            '                    self.cached_rows.push(emitted.clone());',
            '                    self.buffer_count += 1;',
            '',
            '                    if self.buffer_count >= self.buffer {',
            '                        self.processed_rows = emit_idx;',
            '                        if let Some(ref p) = cache_path { self.save_cache(p); }',
            '                        self.cached_rows.clear();',
            '                        self.buffer_count = 0;',
            '                    }',
            '',
            '                    return Some(emitted);',
            '                }',
            '            }',
            '',
            '            // Flush remaining lookahead buffer',
            '            while !self.lookahead_buffer.is_empty() {',
            '                let (emitted, emit_idx) = self.lookahead_buffer.remove(0);',
            '',
            '                self.cached_rows.push(emitted.clone());',
            '                self.buffer_count += 1;',
            '',
            '                if self.buffer_count >= self.buffer {',
            '                    self.processed_rows = emit_idx;',
            '                    if let Some(ref p) = cache_path { self.save_cache(p); }',
            '                    self.cached_rows.clear();',
            '                    self.buffer_count = 0;',
            '                }',
            '',
            '                return Some(emitted);',
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

    def _generate_rust_iterator_with_neighbor_filters(self) -> List[str]:
        neighbor_filters = self.get_neighbor_filters()
        lines = [
            '        fn next_row(&mut self) -> Option<Row> {',
            '            let cache_path = self.get_cache_path();',
            '',
            '            // Initialize pending/prev state if needed',
            '            let mut pending_row: Option<BTreeMap<String, String>> = self.pending_row.take();',
            '            let mut pending_idx: Option<usize> = self.pending_idx.take();',
            '            let mut prev_row: Option<BTreeMap<String, String>> = self.prev_row.take();',
            '',
            '            // Replay to current state',
            '            for i in 0..self.current_idx.min(self.raw_rows.len()) {',
            '                if pending_row.is_some() && pending_idx == Some(i.saturating_sub(1)) {',
            '                    let next = &self.raw_rows[i];',
            '                    let mut keep = true;',
        ]

        for i, nf in enumerate(neighbor_filters):
            nf_type = nf.get('type')
            if nf_type == 'next_row_condition':
                lines.append(f'                    keep = keep && self.check_neighbor_filter_{i}(pending_row.as_ref().unwrap(), Some(next));')
            elif nf_type == 'consecutive_duplicate':
                lines.append(f'                    keep = keep && self.check_neighbor_filter_{i}(pending_row.as_ref().unwrap(), prev_row.as_ref());')

        lines.extend([
            '                    if keep && self.should_keep_row(pending_row.as_ref().unwrap()) { /* row was kept */ }',
            '                    pending_row = None;',
            '                }',
            '                prev_row = Some(self.raw_rows[i].clone());',
            '                pending_row = Some(self.raw_rows[i].clone());',
            '                pending_idx = Some(i);',
            '            }',
            '',
            '            // Continue processing',
            '            while self.current_idx < self.raw_rows.len() {',
            '                if self.current_idx < self.processed_rows {',
            '                    self.current_idx += 1;',
            '                    continue;',
            '                }',
            '',
            '                let raw_row = self.raw_rows[self.current_idx].clone();',
            '',
            '                // Process pending row if exists',
            '                if pending_row.is_some() {',
            '                    let mut keep = true;',
        ])

        for i, nf in enumerate(neighbor_filters):
            nf_type = nf.get('type')
            if nf_type == 'next_row_condition':
                lines.append(f'                    keep = keep && self.check_neighbor_filter_{i}(pending_row.as_ref().unwrap(), Some(&raw_row));')
            elif nf_type == 'consecutive_duplicate':
                lines.append(f'                    keep = keep && self.check_neighbor_filter_{i}(pending_row.as_ref().unwrap(), prev_row.as_ref());')

        lines.extend([
            '                    if keep && self.should_keep_row(pending_row.as_ref().unwrap()) {',
            '                        let result = self.transform_row(pending_row.as_ref().unwrap());',
            '                        self.cached_rows.push(result.clone());',
            '                        self.buffer_count += 1;',
            '                        self.processed_rows = self.current_idx;',
            '                        if let Some(ref p) = cache_path { self.save_cache(p); }',
            '',
            '                        if self.buffer_count >= self.buffer {',
            '                            self.cached_rows.clear();',
            '                            self.buffer_count = 0;',
            '                        }',
            '',
            '                        pending_row = Some(raw_row.clone());',
            '                        pending_idx = Some(self.current_idx);',
            '                        prev_row = Some(raw_row.clone());',
            '                        self.current_idx += 1;',
            '',
            '                        // Save state before returning',
            '                        self.pending_row = pending_row;',
            '                        self.pending_idx = pending_idx;',
            '                        self.prev_row = prev_row;',
            '',
            '                        return Some(result);',
            '                    }',
            '                    pending_row = None;',
            '                }',
            '',
            '                // Set current as pending',
            '                pending_row = Some(raw_row.clone());',
            '                pending_idx = Some(self.current_idx);',
            '                prev_row = Some(raw_row.clone());',
            '                self.current_idx += 1;',
            '            }',
            '',
            '            // Handle last pending row',
            '            if pending_row.is_some() {',
            '                let mut keep = true;',
        ])

        for i, nf in enumerate(neighbor_filters):
            nf_type = nf.get('type')
            if nf_type == 'next_row_condition':
                lines.append(f'                keep = keep && self.check_neighbor_filter_{i}(pending_row.as_ref().unwrap(), None);')
            elif nf_type == 'consecutive_duplicate':
                lines.append(f'                keep = keep && self.check_neighbor_filter_{i}(pending_row.as_ref().unwrap(), prev_row.as_ref());')

        lines.extend([
            '                if keep && self.should_keep_row(pending_row.as_ref().unwrap()) {',
            '                    let result = self.transform_row(pending_row.as_ref().unwrap());',
            '                    self.cached_rows.push(result.clone());',
            '                    self.processed_rows = self.current_idx;',
            '                    if let Some(ref p) = cache_path { self.save_cache(p); }',
            '                    pending_row = None;',
            '',
            '                    self.pending_row = None;',
            '                    self.pending_idx = None;',
            '                    self.prev_row = None;',
            '',
            '                    return Some(result);',
            '                }',
            '            }',
            '',
            '            if self.buffer_count > 0 {',
            '                if let Some(ref p) = cache_path { self.save_cache(p); }',
            '                self.cached_rows.clear();',
            '            }',
            '',
            '            self.pending_row = None;',
            '            self.pending_idx = None;',
            '            self.prev_row = None;',
            '',
            '            None',
            '        }',
        ])

        return lines
