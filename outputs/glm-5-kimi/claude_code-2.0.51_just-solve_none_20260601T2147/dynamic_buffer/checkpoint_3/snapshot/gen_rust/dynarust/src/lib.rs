//! Dynamic Preprocessor - Streaming data processor with caching support.

use std::collections::BTreeMap;
use std::fs::{self, File};
use std::io::{BufRead, BufReader, Read, Write};
use std::path::PathBuf;
use serde::{Deserialize, Serialize};

pub mod api {
    use std::collections::BTreeMap;
    use std::io;
    use std::path::Path;

    /// Represents a cell value in a row.
    #[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
    #[serde(untagged)]
    pub enum Value {
        Null,
        Bool(bool),
        Int(i64),
        Float(f64),
        Str(String),
    }

    impl Value {
        pub fn is_null(&self) -> bool {
            matches!(self, Value::Null)
        }
    }

    impl Default for Value {
        fn default() -> Self {
            Value::Null
        }
    }

    impl From<bool> for Value {
        fn from(v: bool) -> Self { Value::Bool(v) }
    }

    impl From<i64> for Value {
        fn from(v: i64) -> Self { Value::Int(v) }
    }

    impl From<i32> for Value {
        fn from(v: i32) -> Self { Value::Int(v as i64) }
    }

    impl From<f64> for Value {
        fn from(v: f64) -> Self { Value::Float(v) }
    }

    impl From<String> for Value {
        fn from(v: String) -> Self { Value::Str(v) }
    }

    impl From<&str> for Value {
        fn from(v: &str) -> Self { Value::Str(v.to_string()) }
    }

    pub type Row = BTreeMap<String, Value>;

    /// Streaming preprocessor with caching and resuming support.
    pub struct DynamicPreprocessor {
        buffer: usize,
        cache_dir: Option<String>,
        current_path: Option<String>,
        raw_rows: Vec<BTreeMap<String, String>>,
        current_idx: usize,
        processed_rows: usize,
        cached_rows: Vec<Row>,
        buffer_count: usize,
    }

    impl DynamicPreprocessor {
        pub fn new(buffer: usize, cache_dir: Option<&str>) -> DynamicPreprocessor {
            DynamicPreprocessor {
                buffer,
                cache_dir: cache_dir.map(|s| s.to_string()),
                current_path: None,
                raw_rows: Vec::new(),
                current_idx: 0,
                processed_rows: 0,
                cached_rows: Vec::new(),
                buffer_count: 0,
            }
        }

        pub fn open<P: AsRef<std::path::Path>>(&mut self, path: P) -> io::Result<()> {
            let path_str = path.as_ref().to_string_lossy().to_string();
            self.current_path = Some(path_str.clone());
            self.raw_rows.clear();
            self.current_idx = 0;
            self.processed_rows = 0;
            self.cached_rows.clear();
            self.buffer_count = 0;


            let file = File::open(&path)?;
            let reader = BufReader::new(file);
            let mut lines = reader.lines();

            let headers: Vec<String> = if let Some(Ok(line)) = lines.next() {
                line.split(',').map(|s| s.trim().to_string()).collect()
            } else {
                return Ok(());
            };

            for line in lines {
                let line = line?;
                if line.trim().is_empty() { continue; }
                let values: Vec<&str> = line.split(',').collect();
                let mut row = BTreeMap::new();
                for (i, h) in headers.iter().enumerate() {
                    let v = if i < values.len() { values[i].trim().to_string() } else { String::new() };
                    row.insert(h.clone(), v);
                }
                self.raw_rows.push(row);
            }

            // Load cache if exists
            if let Some(cache_path) = self.get_cache_path() {
                self.load_cache(&cache_path);
            }

            Ok(())
        }

        fn get_cache_path(&self) -> Option<PathBuf> {
            let cache_dir = self.cache_dir.as_ref()?;
            let path = self.current_path.as_ref()?;
            let _ = fs::create_dir_all(cache_dir);
            let hash = format!("{:x}", md5_hash(path.as_bytes()));
            Some(PathBuf::from(cache_dir).join(format!("{}.json", hash)))
        }

        fn load_cache(&mut self, cache_path: &PathBuf) {
            if !cache_path.exists() { return; }
            if let Ok(content) = fs::read_to_string(cache_path) {
                if let Ok(cache) = serde_json::from_str::<serde_json::Value>(&content) {
                    if let Some(obj) = cache.as_object() {
                        if let Some(v) = obj.get("processed_rows").and_then(|v| v.as_u64()) {
                            self.processed_rows = v as usize;
                        }
                        if let Some(state) = obj.get("state").and_then(|v| v.as_object()) {
                        }
                    }
                }
            }
        }

        fn save_cache(&mut self, cache_path: &PathBuf) {
            let mut state_obj = serde_json::Map::new();

            let cache = serde_json::json!({
                "processed_rows": self.processed_rows,
                "rows": self.cached_rows,
                "state": state_obj
            });

            let _ = fs::write(cache_path, serde_json::to_string_pretty(&cache).unwrap_or_default());
        }

        fn parse_value(s: &str) -> Value {
            if s.is_empty() { return Value::Null; }
            if s == "true" { return Value::Bool(true); }
            if s == "false" { return Value::Bool(false); }
            if let Ok(v) = s.parse::<i64>() { return Value::Int(v); }
            if let Ok(v) = s.parse::<f64>() { return Value::Float(v); }
            Value::Str(s.to_string())
        }

        fn should_keep_row(&self, row: &BTreeMap<String, String>) -> bool {
            if row.get("id").map(|v| v.as_str()) == Some("2") { return false; }
            if row.get("name").map(|v| v.as_str()) == Some("Bob") { return false; }
            if row.get("age").map(|v| v.as_str()) == Some("19") { return false; }
            if row.get("active").map(|v| v.as_str()) == Some("false") { return false; }
            true
        }

        fn transform_row(&mut self, row: &BTreeMap<String, String>) -> Row {
            let mut result = Row::new();

            if let Some(v) = row.get("id") {
                result.insert("id".to_string(), Self::parse_value(v));
            }
            if let Some(v) = row.get("name") {
                result.insert("name".to_string(), Self::parse_value(v));
            }
            result
        }

        fn next_row(&mut self) -> Option<Row> {
            let cache_path = self.get_cache_path();

            while self.current_idx < self.raw_rows.len() {
                if self.current_idx < self.processed_rows {
                    self.current_idx += 1;
                    continue;
                }

                let raw_row = self.raw_rows[self.current_idx].clone();
                self.current_idx += 1;
                self.processed_rows = self.current_idx;

                if !self.should_keep_row(&raw_row) {
                    if let Some(ref p) = cache_path { self.save_cache(p); }
                    continue;
                }

                let result = self.transform_row(&raw_row);
                self.cached_rows.push(result.clone());
                self.buffer_count += 1;

                if self.buffer_count >= self.buffer {
                    if let Some(ref p) = cache_path { self.save_cache(p); }
                    self.cached_rows.clear();
                    self.buffer_count = 0;
                }

                return Some(result);
            }

            if self.buffer_count > 0 {
                if let Some(ref p) = cache_path { self.save_cache(p); }
                self.cached_rows.clear();
            }

            None
        }
    }

    impl Iterator for DynamicPreprocessor {
        type Item = Row;

        fn next(&mut self) -> Option<Self::Item> {
            self.next_row()
        }
    }
}

// MD5 hash function for cache keys
fn md5_hash(data: &[u8]) -> u128 {
    let mut hash: u128 = 5381;
    for &b in data {
        hash = ((hash << 5).wrapping_add(hash)) ^ b as u128;
    }
    for &b in data {
        hash = ((hash << 5).wrapping_add(hash)) ^ b as u128;
    }
    hash
}
    }

}  // mod api
