#!/usr/bin/env python3
"""
Dynamic Buffer Generator - Extended for Stateful Transforms (Part 2)

Generates a DynamicPreprocessor module by inferring transformations from sample
input/output files. Supports stateful transforms:
- Prefix/cumulative transforms
- Sliding window transforms
- State-machine sequence labeling
- Neighbor-based filtering

All transforms are deterministic, streaming-friendly, and cache/resume-safe.
"""

import csv
import json
import argparse
import hashlib
from pathlib import Path
from typing import Dict, List, Optional, Any, Set, Tuple, Deque
from collections import defaultdict, deque
from enum import Enum


SUPPORTED_EXTS = {"csv", "tsv", "jsonl", "json"}


class TransformType(Enum):
    CONSTANT = "constant"
    COPY = "copy"
    NUMERIC_LINEAR = "numeric_linear"
    DROPPED = "dropped"
    PREFIX_SUM = "prefix_sum"
    PREFIX_COUNT = "prefix_count"
    WINDOW_SUM = "window_sum"
    WINDOW_AVG = "window_avg"
    SEGMENT_ID = "segment_id"


class SampleParser:
    """Parse and load sample data files."""

    def __init__(self, sample_dir: str):
        self.sample_dir = Path(sample_dir)
        self.input_path: Optional[Path] = None
        self.output_path: Optional[Path] = None
        self.extension: Optional[str] = None
        self._find_files()

    def _find_files(self):
        for ext in SUPPORTED_EXTS:
            input_file = self.sample_dir / f"input.{ext}"
            output_file = self.sample_dir / f"output.{ext}"
            if input_file.exists() and output_file.exists():
                self.input_path = input_file
                self.output_path = output_file
                self.extension = ext
                return
        raise ValueError(f"No valid input/output pair found. Expected extensions: {SUPPORTED_EXTS}")

    def load_samples(self) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        if self.extension in ("csv", "tsv"):
            return (self._load_delimited(self.input_path, self.extension == "tsv"),
                    self._load_delimited(self.output_path, self.extension == "tsv"))
        elif self.extension == "jsonl":
            return self._load_jsonl(self.input_path), self._load_jsonl(self.output_path)
        else:
            return self._load_json_array(self.input_path), self._load_json_array(self.output_path)

    def _load_delimited(self, filepath: Path, is_tsv: bool) -> List[Dict[str, Any]]:
        rows = []
        delimiter = "\t" if is_tsv else ","
        with open(filepath, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter=delimiter)
            for row in reader:
                rows.append({k: self._convert(v) for k, v in row.items()})
        return rows

    def _load_jsonl(self, filepath: Path) -> List[Dict[str, Any]]:
        rows = []
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return rows

    def _load_json_array(self, filepath: Path) -> List[Dict[str, Any]]:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError(f"Expected JSON array, got {type(data)}")
        return data

    def _convert(self, v: str) -> Any:
        if not v:
            return v
        v_lower = v.lower()
        if v_lower == "true":
            return True
        if v_lower == "false":
            return False
        try:
            return int(v)
        except ValueError:
            pass
        try:
            return float(v)
        except ValueError:
            pass
        return v


class StatefulInferrer:
    """Infer stateful transformations from sample input/output pairs."""

    def __init__(self, input_rows: List[Dict], output_rows: List[Dict], ext: str):
        self.input_rows = input_rows
        self.output_rows = output_rows
        self.extension = ext
        self._input_cols = list(input_rows[0].keys()) if input_rows else []
        self._output_cols = set()
        self._filter_type: Optional[str] = None
        self._filter_info: Dict = {}
        self._stateful_transforms: Dict[str, Dict] = {}
        self._simple_transforms: Dict[str, Dict] = {}
        self._kept_indices: Set[int] = set()
        self._match_output_to_input()
        self._infer_filter()
        self._infer_transforms()

    def _match_output_to_input(self):
        for row in self.output_rows:
            self._output_cols.update(row.keys())

        # Match output rows to input rows
        for out_row in self.output_rows:
            for i, in_row in enumerate(self.input_rows):
                # Direct match
                if self._rows_match(in_row, out_row):
                    self._kept_indices.add(i)
                    break

        # Also include rows that are partial matches (for stateful transforms)
        for i, in_row in enumerate(self.input_rows):
            if i in self._kept_indices:
                continue
            for out_row in self.output_rows:
                # If most columns match (excluding new stateful cols)
                matching = sum(1 for k in in_row if k in out_row and self._values_eq(in_row[k], out_row[k]))
                if matching >= len(in_row) - 1:
                    self._kept_indices.add(i)
                    break

    def _rows_match(self, r1: Dict, r2: Dict) -> bool:
        for k, v in r2.items():
            if k not in r1:
                return False
            if not self._values_eq(r1[k], v):
                return False
        return True

    def _values_eq(self, v1: Any, v2: Any) -> bool:
        if v1 == v2:
            return True
        if isinstance(v1, (int, float)) and isinstance(v2, (int, float)):
            return abs(v1 - v2) < 1e-9
        return False

    def _infer_filter(self):
        dropped = [i for i in range(len(self.input_rows)) if i not in self._kept_indices]
        if not dropped:
            self._filter_type = "none"
            return

        # Try to infer neighbor-based filter
        neighbor = self._infer_neighbor_filter()
        if neighbor:
            self._filter_type = "neighbor"
            self._filter_info = neighbor
        else:
            self._filter_type = "column"
            self._filter_info = self._infer_column_filter()

    def _infer_neighbor_filter(self) -> Optional[Dict]:
        # Check for "keep_first_of_duplicates" pattern
        if not self._input_cols:
            return None

        key_col = self._input_cols[0]
        groups = []
        current = []

        for i, row in enumerate(self.input_rows):
            val = row.get(key_col)
            if current and val == self.input_rows[current[0]].get(key_col):
                current.append(i)
            else:
                if len(current) > 1:
                    groups.append(current)
                current = [i]
        if len(current) > 1:
            groups.append(current)

        for group in groups:
            if group[0] in self._kept_indices and all(i not in self._kept_indices for i in group[1:]):
                return {"pattern": "keep_first_of_duplicates", "field": key_col}

        # Check for "drop_if_next_has_status_duplicate"
        if "status" in self._input_rows[0]:
            matches = 0
            total = 0
            for i in range(len(self.input_rows) - 1):
                if self.input_rows[i+1].get("status") == "duplicate":
                    total += 1
                    if i not in self._kept_indices:
                        matches += 1
            if total > 0 and matches >= total * 0.5:
                return {"pattern": "drop_if_next_duplicate", "field": "status"}

        return None

    def _infer_column_filter(self) -> Dict:
        kept_vals_per_col = defaultdict(list)
        dropped_vals_per_col = defaultdict(list)

        for i, row in enumerate(self.input_rows):
            d = kept_vals_per_col if i in self._kept_indices else dropped_vals_per_col
            for k, v in row.items():
                d[k].append(v)

        filters = []
        for col in self._input_cols:
            kept = kept_vals_per_col.get(col, [])
            dropped = dropped_vals_per_col.get(col, [])
            if not kept or not dropped:
                continue
            if len(set(kept)) == 1 and kept[0] not in dropped:
                filters.append(("eq", col, kept[0]))
            elif len(set(dropped)) == 1 and dropped[0] not in kept:
                filters.append(("ne", col, dropped[0]))
            elif all(isinstance(v, (int, float)) for v in kept + dropped):
                min_kept, max_dropped = min(kept), max(dropped)
                if min_kept > max_dropped:
                    filters.append(("gt", col, max_dropped))

        return {"filters": filters}

    def _infer_transforms(self):
        for col in self._output_cols:
            if col in self._input_cols:
                # Could be copy or transformed
                st = self._infer_stateful(col)
                if st:
                    self._stateful_transforms[col] = st
                elif self._is_constant(col):
                    self._simple_transforms[col] = {"type": "constant", "value": self._get_first_output(col)}
                elif self._is_linear_transform(col):
                    a, b = self._infer_linear_coef(col)
                    self._simple_transforms[col] = {"type": "numeric_linear", "a": a, "b": b}
                else:
                    self._simple_transforms[col] = {"type": "copy", "source": col}
            else:
                # New column - must be stateful or constant
                st = self._infer_stateful(col)
                if st:
                    self._stateful_transforms[col] = st
                elif self._is_constant(col):
                    self._simple_transforms[col] = {"type": "constant", "value": self._get_first_output(col)}
                else:
                    self._simple_transforms[col] = {"type": "derived"}

    def _get_first_output(self, col: str) -> Any:
        for row in self.output_rows:
            if col in row:
                return row[col]
        return None

    def _is_constant(self, col: str) -> bool:
        vals = [row.get(col) for row in self.output_rows if col in row]
        if not vals:
            return False
        return all(v == vals[0] for v in vals)

    def _is_linear_transform(self, col: str) -> bool:
        pairs = []
        for row_o, row_i in zip(self.output_rows, self.input_rows):
            if col in row_o and self._input_cols[0] in row_i:
                x = row_i[self._input_cols[0]]
                y = row_o[col]
                if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                    pairs.append((x, y))
        if len(pairs) < 2:
            return False
        x1, y1 = pairs[0]
        x2, y2 = pairs[1]
        if x2 == x1:
            return False
        a = (y2 - y1) / (x2 - x1)
        b = y1 - a * x1
        return all(abs(a * x + b - y) < 1e-6 for x, y in pairs)

    def _infer_linear_coef(self, col: str) -> tuple:
        pairs = [(row_i[self._input_cols[0]], row_o[col])
                 for row_o, row_i in zip(self.output_rows, self.input_rows)
                 if col in row_o and self._input_cols[0] in row_i
                 and isinstance(row_i[self._input_cols[0]], (int, float))
                 and isinstance(row_o[col], (int, float))]
        x1, y1 = pairs[0]
        x2, y2 = pairs[1]
        a = (y2 - y1) / (x2 - x1)
        b = y1 - a * x1
        return a, b

    def _infer_stateful(self, col: str) -> Optional[Dict]:
        if not self.output_rows:
            return None

        # Check for prefix sum/count
        prefix = self._check_prefix_transform(col)
        if prefix:
            return prefix

        # Check for sliding window
        window = self._check_sliding_window(col)
        if window:
            return window

        # Check for segment_id pattern
        segment = self._check_segment_id(col)
        if segment:
            return segment

        return None

    def _check_prefix_transform(self, col: str) -> Optional[Dict]:
        in_col = self._input_cols[0] if self._input_cols else "value"
        pairs = [(row_i.get(in_col), row_o.get(col))
                 for row_o, row_i in zip(self.output_rows, self.input_rows)
                 if in_col in row_i and col in row_o
                 and isinstance(row_i.get(in_col), (int, float))
                 and isinstance(row_o.get(col), (int, float))]
        if len(pairs) < 2:
            return None

        # Check prefix sum
        running = 0
        sum_ok = True
        for iv, ov in pairs:
            running += iv
            if abs(running - ov) > 1e-6:
                sum_ok = False
                break
        if sum_ok:
            return {"type": "stateful", "kind": "prefix_sum", "source": in_col}

        # Check prefix count
        count_ok = all(i + 1 == pairs[i][1] for i in range(len(pairs)))
        if count_ok:
            return {"type": "stateful", "kind": "prefix_count", "source": in_col}

        return None

    def _check_sliding_window(self, col: str) -> Optional[Dict]:
        in_col = self._input_cols[0] if self._input_cols else "value"
        pairs = [(row_i.get(in_col), row_o.get(col))
                 for row_o, row_i in zip(self.output_rows, self.input_rows)
                 if in_col in row_i and col in row_o
                 and isinstance(row_i.get(in_col), (int, float))
                 and isinstance(row_o.get(col), (int, float))]
        if len(pairs) < 2:
            return None

        for window_size in range(1, min(65, len(pairs) + 1)):
            # Check if output matches window sum
            sum_ok = True
            for i in range(window_size - 1, len(pairs)):
                window = [p[0] for p in pairs[i-window_size+1:i+1]]
                expected = sum(window)
                if abs(expected - pairs[i][1]) > 1e-6:
                    sum_ok = False
                    break
            if sum_ok:
                return {"type": "stateful", "kind": "window_sum", "source": in_col, "window_size": window_size}

            # Check if output matches window average
            avg_ok = True
            for i in range(window_size - 1, len(pairs)):
                window = [p[0] for p in pairs[i-window_size+1:i+1]]
                expected = sum(window) / len(window)
                if abs(expected - pairs[i][1]) > 1e-6:
                    avg_ok = False
                    break
            if avg_ok:
                return {"type": "stateful", "kind": "window_avg", "source": in_col, "window_size": window_size}

        return None

    def _check_segment_id(self, col: str) -> Optional[Dict]:
        vals = [row.get(col) for row in self.output_rows if col in row]
        if not vals or any(v is None for v in vals):
            return None

        try:
            int_vals = [int(v) for v in vals]
        except (ValueError, TypeError):
            return None

        # Check for segment_id pattern
        changes = 0
        for i in range(1, len(int_vals)):
            if int_vals[i] < int_vals[i-1]:
                return None  # Should not decrease
            if int_vals[i] > int_vals[i-1]:
                if int_vals[i] - int_vals[i-1] > 1:
                    return None  # Should increment by at most 1
                changes += 1

        if changes > 0:
            return {"type": "stateful", "kind": "segment_id", "source": self._input_cols[0]}

        return None


class CodeGenerator:
    """Generate DynamicPreprocessor code in target language."""

    def __init__(self, module_name: str, inferrer: StatefulInferrer, ext: str):
        self.module_name = module_name
        self.inferrer = inferrer
        self.extension = ext

    def generate_python(self) -> Dict[str, str]:
        return {
            f"{self.module_name}/__init__.py": self._python_init(),
            f"{self.module_name}/{self.module_name}.py": self._python_module(),
        }

    def generate_javascript(self) -> Dict[str, str]:
        return {f"{self.module_name}.js": self._javascript_module()}

    def _python_init(self) -> str:
        return f'from .{self.module_name} import DynamicPreprocessor\n__all__ = ["DynamicPreprocessor"]\n'

    def _python_module(self) -> str:
        parser_code = self._python_parsers()
        filter_code = self._python_filter_code()
        transform_code = self._python_transform_code()
        state_code = self._python_state_code()
        filter_method = self._python_filter_method()
        stateful_method = self._python_stateful_method()

        return f'''"""
Auto-generated DynamicPreprocessor module for {self.module_name}.

This module provides a streaming preprocessor with stateful transform support:
1. Reads {self.extension.upper()} files with the same schema as the sample
2. Applies the inferred transformation row-by-row
3. Supports caching and resuming with state persistence
"""

import csv
import json
import hashlib
from pathlib import Path
from typing import Dict, Iterator, Any, Optional, List, Deque
from collections import defaultdict, deque


class TransformState:
    """Serializable state for streaming transforms."""

    def __init__(self):
        self.prefix_sum = defaultdict(float)
        self.prefix_count = defaultdict(int)
        self.windows: Dict[str, Deque] = {}
        self.segment_id: Dict[str, int] = {}
        self.deferred_row: Optional[Dict] = None
        self.window_sizes: Dict[str, int] = {}
        self.processed_count = 0

    def to_dict(self) -> Dict:
        return {{
            "prefix_sum": dict(self.prefix_sum),
            "prefix_count": dict(self.prefix_count),
            "windows": {{k: list(v) for k, v in self.windows.items()}},
            "segment_id": dict(self.segment_id),
            "window_sizes": self.window_sizes,
            "processed_count": self.processed_count,
        }}

    @classmethod
    def from_dict(cls, data: Dict) -> "TransformState":
        state = cls()
        state.prefix_sum = defaultdict(float, data.get("prefix_sum", {{}}))
        state.prefix_count = defaultdict(int, data.get("prefix_count", {{}}))
        state.windows = {{k: deque(v) for k, v in data.get("windows", {{}}).items()}}
        state.segment_id = data.get("segment_id", {{}})
        state.window_sizes = data.get("window_sizes", {{}})
        state.processed_count = data.get("processed_count", 0)
        return state

    def clear(self):
        self.prefix_sum.clear()
        self.prefix_count.clear()
        self.windows.clear()
        self.segment_id.clear()
        self.window_sizes.clear()
        self.processed_count = 0


{parser_code}


class DynamicPreprocessor:
    """
    Dynamic preprocessor with stateful transform support.

    Args:
        buffer: Maximum number of rows to keep in memory at once
        cache_dir: Optional directory for caching processed rows and state
    """

    def __init__(self, buffer: int, cache_dir: Optional[str] = None):
        self.buffer = buffer
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._parser_map = {{
            "csv": CSVParser,
            "tsv": TSVParser,
            "jsonl": JSONLParser,
            "json": JSONParser,
        }}
        self._extension = "{self.extension}"
        self._state = TransformState()

    def __call__(self, path: str) -> Iterator[Dict[str, Any]]:
        return self._process(Path(path))

    def _process(self, path: Path) -> Iterator[Dict[str, Any]]:
        parser_class = self._parser_map[self._extension]
        parser = parser_class(path)

        if self.cache_dir is None:
            yield from self._process_direct(parser)
        else:
            yield from self._process_with_cache(parser, path)

    def _process_direct(self, parser):
        buffer = []
        self._state.clear()

        for row in parser.parse():
            # Apply filter
            if not self._process_filter(row):
                continue

            # Transform with state
            transformed = self._transform_with_state(row)
            if transformed is not None:
                buffer.append(transformed)
                if len(buffer) >= self.buffer:
                    yield from buffer
                    buffer.clear()

        yield from buffer

    def _process_with_cache(self, parser, path: Path):
        cache_file = self._get_cache_file(path)
        cache = self._load_cache(cache_file)

        # Load persisted state
        saved_state = cache.get("transform_state")
        if saved_state:
            self._state = TransformState.from_dict(saved_state)

        # Start from where we left off
        start_idx = cache.get("processed_count", 0)
        buffer = []

        # Yield already processed rows
        for row in cache.get("processed_rows", []):
            yield row

        for i, row in enumerate(parser.parse()):
            if i < start_idx:
                continue

            if not self._process_filter(row):
                continue

            transformed = self._transform_with_state(row)
            if transformed is not None:
                buffer.append(transformed)
                cache.setdefault("processed_rows", []).append(transformed)

                if len(buffer) >= self.buffer:
                    yield from buffer
                    self._save_cache(cache_file, cache)
                    buffer.clear()

        yield from buffer
        cache.setdefault("processed_rows", []).extend(buffer)
        cache["transform_state"] = self._state.to_dict()
        cache["processed_count"] = start_idx + len(cache["processed_rows"]) - start_idx
        cache["complete"] = True
        self._save_cache(cache_file, cache)

    def _transform_with_state(self, row: Dict) -> Optional[Dict]:
        transformed = {}
{filter_code}
{transform_code}
{stateful_method}
        return transformed

    def _get_cache_file(self, path: Path) -> Path:
        key = f"{{path.parent.name}}_{{path.name}}_{{self._extension}}"
        hash_part = hashlib.md5(key.encode()).hexdigest()[:8]
        return self.cache_dir / f"{{hash_part}}_cache.json"

    def _load_cache(self, cache_file: Path) -> Dict:
        if cache_file.exists():
            try:
                with open(cache_file) as f:
                    return json.load(f)
            except:
                pass
        return {{
            "processed_rows": [],
            "transform_state": {{}},
            "processed_count": 0,
            "complete": False,
        }}

    def _save_cache(self, cache_file: Path, cache: Dict):
        if not self.cache_dir:
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        with open(cache_file, "w") as f:
            json.dump(cache, f, default=str, indent=2)

{filter_method}
'''

    def _python_filter_code(self) -> str:
        if self.inferrer._filter_type == "none":
            return "        # No filtering"
        elif self.inferrer._filter_type == "column":
            parts = []
            for op, col, val in self.inferrer._filter_info.get("filters", []):
                if op == "eq":
                    parts.append(f'row.get("{col}") == {repr(val)}')
                elif op == "ne":
                    parts.append(f'row.get("{col}") != {repr(val)}')
                elif op == "gt":
                    parts.append(f'row.get("{col}") > {val}')
            return f"        if not ({' and '.join(parts)}): return None"
        elif self.inferrer._filter_type == "neighbor":
            info = self.inferrer._filter_info
            pattern = info.get("pattern")
            if pattern == "keep_first_of_duplicates":
                field = info.get("field", "")
                return f'''
        # Neighbor filter: keep first of consecutive duplicates
        val = row.get("{field}")
        self._state.deferred_row = row
        if hasattr(self._state, 'last_{field}_val'):
            if val != getattr(self._state, f'last_{field}_val'):
                result = self._state.deferred_row
                self._state.deferred_row = None
                setattr(self._state, f'last_{field}_val', val)
                return result
            else:
                return None
        setattr(self._state, f'last_{field}_val', val)
        '''
            elif pattern == "drop_if_next_duplicate":
                return f'''
        # Neighbor filter: drop if next row has status='duplicate'
        self._state.deferred_row = row
        return None
        '''
        return "        # Filter inferred"

    def _python_filter_method(self) -> str:
        if self.inferrer._filter_type == "column":
            parts = []
            for op, col, val in self.inferrer._filter_info.get("filters", []):
                if op == "eq":
                    parts.append(f'row.get("{col}") == {repr(val)}')
                elif op == "ne":
                    parts.append(f'row.get("{col}") != {repr(val)}')
                elif op == "gt":
                    parts.append(f'row.get("{col}") > {val}')
            return f'''    def _process_filter(self, row: Dict) -> bool:
        return {' and '.join(parts)}
'''
        elif self.inferrer._filter_type == "neighbor":
            info = self.inferrer._filter_info
            pattern = info.get("pattern")
            if pattern == "keep_first_of_duplicates":
                field = info.get("field", "")
                return f'''
    def _process_filter(self, row: Dict) -> bool:
        # Neighbor filter: keep first of consecutive duplicates
        val = row.get("{field}")
        if hasattr(self._state, f'last_{field}_val'):
            if val != getattr(self._state, f'last_{field}_val'):
                result = True
                setattr(self._state, f'last_{field}_val', val)
                return result
            return False
        setattr(self._state, f'last_{field}_val', val)
        return True
'''
            elif pattern == "drop_if_next_duplicate":
                return '''
    def _process_filter(self, row: Dict) -> bool:
        # Neighbor filter: deferred - handled in transform
        return True
'''
        return '''    def _process_filter(self, row: Dict) -> bool:
        return True
'''

    def _python_transform_code(self) -> str:
        code = []
        for col, transform in self.inferrer._simple_transforms.items():
            if transform["type"] == "constant":
                val = transform["value"]
                if isinstance(val, bool):
                    val = str(val).lower()
                code.append(f'        transformed["{col}"] = {repr(val)}')
            elif transform["type"] == "copy":
                src = transform["source"]
                code.append(f'        transformed["{col}"] = row.get("{src}")')
            elif transform["type"] == "numeric_linear":
                a, b = transform["a"], transform["b"]
                src = self.inferrer._input_cols[0]
                code.append(f'        transformed["{col}"] = {a} * (row.get("{src}") or 0) + {b}')
            elif transform["type"] == "dropped":
                pass
            else:
                code.append(f'        transformed["{col}"] = row.get("{col}")')

        return "\n".join(code)

    def _python_state_code(self) -> str:
        code = []
        stateful = self.inferrer._stateful_transforms

        if stateful:
            for col, info in stateful.items():
                kind = info.get("kind")
                src = info.get("source", "value")

                if kind == "prefix_sum":
                    code.append(f'''
        # Prefix sum for '{col}'
        val = row.get("{src}") or 0
        if isinstance(val, (int, float)):
            self._state.prefix_sum["{col}"] += val
            transformed["{col}"] = self._state.prefix_sum["{col}"]
''')
                elif kind == "prefix_count":
                    code.append(f'''
        # Prefix count for '{col}'
        self._state.prefix_count["{col}"] += 1
        transformed["{col}"] = self._state.prefix_count["{col}"]
''')
                elif kind == "window_sum":
                    ws = info.get("window_size", 3)
                    key = f"{col}_{src}"
                    code.append(f'''
        # Sliding window sum for '{col}' (size={ws})
        val = row.get("{src}") or 0
        if isinstance(val, (int, float)):
            w = self._state.windows.get("{key}", deque())
            if len(w) >= {ws}:
                w.popleft()
            w.append(val)
            self._state.windows["{key}"] = w
            self._state.window_sizes["{key}"] = {ws}
            transformed["{col}"] = sum(w)
''')
                elif kind == "window_avg":
                    ws = info.get("window_size", 3)
                    key = f"{col}_{src}"
                    code.append(f'''
        # Sliding window average for '{col}' (size={ws})
        val = row.get("{src}") or 0
        if isinstance(val, (int, float)):
            w = self._state.windows.get("{key}", deque())
            if len(w) >= {ws}:
                w.popleft()
            w.append(val)
            self._state.windows["{key}"] = w
            self._state.window_sizes["{key}"] = {ws}
            transformed["{col}"] = sum(w) / len(w) if w else 0
''')
                elif kind == "segment_id":
                    src = info.get("source", "")
                    code.append(f'''
        # Segment ID from '{src}'
        val = row.get("{src}", 0)
        seg_key = "{col}_{src}"
        if isinstance(val, (int, float)):
            threshold = 1.0  # Can be refined from sample
            current = self._state.segment_id.get(seg_key, 1)
            if val >= threshold:
                current += 1
                self._state.segment_id[seg_key] = current
            transformed["{col}"] = current
''')
        else:
            code.append("        # No stateful transforms")

        return "".join(code) if code else "        # No stateful transforms"

    def _python_stateful_method(self) -> str:
        code = []
        stateful = self.inferrer._stateful_transforms

        for col, info in stateful.items():
            kind = info.get("kind")
            if kind == "segment_id":
                src = info.get("source", "")
                # Try to infer threshold from sample
                vals = []
                for row in self.inferrer.input_rows:
                    if src in row:
                        val = row[src]
                        if isinstance(val, (int, float)):
                            vals.append(val)

                if vals:
                    # Find a natural threshold
                    avg = sum(vals) / len(vals)
                    median = sorted(vals)[len(vals) // 2]
                    threshold = (avg + median) / 2
                else:
                    threshold = 0

                code.append(f'''
    def _compute_segment_id(self, row: Dict) -> int:
        val = row.get("{src}", {threshold})
        key = "{col}"
        current = self._state.segment_id.get(key, 1)
        if isinstance(val, (int, float)) and val >= {threshold:.4f}:
            current += 1
            self._state.segment_id[key] = current
        return current
''')

        return "\n".join(code) if code else ""

    def _python_parsers(self) -> str:
        return '''
class BaseParser:
    def parse(self):
        raise NotImplementedError


class CSVParser(BaseParser):
    def __init__(self, path: Path):
        self.path = path

    def parse(self):
        with open(self.path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                yield {k: cls._convert(v) for k, v in row.items()}

    @staticmethod
    def _convert(v: str) -> Any:
        if not v:
            return v
        if v.lower() == "true":
            return True
        if v.lower() == "false":
            return False
        try:
            return int(v)
        except ValueError:
            pass
        try:
            return float(v)
        except ValueError:
            pass
        return v


class TSVParser(CSVParser):
    def parse(self):
        with open(self.path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter="\\t")
            for row in reader:
                yield {k: self._convert(v) for k, v in row.items()}


class JSONLParser(BaseParser):
    def __init__(self, path: Path):
        self.path = path

    def parse(self):
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    yield json.loads(line)


class JSONParser(BaseParser):
    def __init__(self, path: Path):
        self.path = path

    def parse(self):
        with open(self.path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            for item in data:
                yield item
'''

    def _javascript_module(self) -> str:
        return '''/**
 * Auto-generated DynamicPreprocessor module for {{module_name}}
 *
 * This module provides a streaming preprocessor with stateful transform support.
 * Note: JavaScript generation for stateful transforms is a simplified version.
 */

const fs = require('fs');
const path = require('path');

class TransformState {
    constructor() {
        this.prefixSum = {};
        this.prefixCount = {};
        this.windows = {};
        this.segmentId = {};
        this.windowSizes = {};
        this.processedCount = 0;
    }

    toDict() {
        return {
            prefixSum: { ...this.prefixSum },
            prefixCount: { ...this.prefixCount },
            windows: Object.fromEntries(
                Object.entries(this.windows).map(([k, v]) => [k, [...v]])
            ),
            segmentId: { ...this.segmentId },
            windowSizes: { ...this.windowSizes },
            processedCount: this.processedCount,
        };
    }

    static fromDict(data) {
        const state = new TransformState();
        state.prefixSum = data.prefixSum || {};
        state.prefixCount = data.prefixCount || {};
        state.windows = Object.fromEntries(
            Object.entries(data.windows || {}).map(([k, v]) => [k, new Array(...v)])
        );
        state.segmentId = data.segmentId || {};
        state.windowSizes = data.windowSizes || {};
        state.processedCount = data.processedCount || 0;
        return state;
    }
}

const parsers = {
    csv: require('csv-parser'),
};

class DynamicPreprocessor {
    constructor(buffer, cache_dir = null) {
        this.buffer = buffer;
        this.cacheDir = cache_dir ? path.resolve(cache_dir) : null;
        this.extension = 'csv';
        this.state = new TransformState();
    }

    process(filePath) {
        const self = this;
        return {
            *[Symbol.iterator]() {
                const results = [];
                fs.createReadStream(filePath)
                    .pipe(parsers.csv())
                    .on('data', (data) => results.push(self._convert(data)))
                    .on('end', () => {});

                if (self.cacheDir === null) {
                    yield* self._processDirect(results);
                } else {
                    yield* self._processWithCache(results, filePath);
                }
            }
        };
    }

    _convert(row) {
        const converted = {};
        for (const [k, v] of Object.entries(row)) {
            if (!v) continue;
            const lower = v.toLowerCase();
            if (lower === 'true') converted[k] = true;
            else if (lower === 'false') converted[k] = false;
            else {
                const num = parseInt(v);
                if (!isNaN(num)) converted[k] = num;
                else {
                    const fnum = parseFloat(v);
                    if (!isNaN(fnum)) converted[k] = fnum;
                    else converted[k] = v;
                }
            }
        }
        return converted;
    }

    _processDirect(rows) {
        const buffer = [];
        this.state = new TransformState();

        for (const row of rows) {
            const transformed = this._transformWithState(row);
            if (transformed !== null) {
                buffer.push(transformed);
                if (buffer.length >= this.buffer) {
                    for (const item of buffer) yield item;
                    buffer.length = 0;
                }
            }
        }
        for (const item of buffer) yield item;
    }

    _processWithCache(rows, filePath) {
        const cacheFile = this._getCacheFile(filePath);
        const cache = this._loadCache(cacheFile);
        this.state = TransformState.fromDict(cache.transformState);

        const buffer = [];
        const startIdx = cache.processedCount || 0;

        for (const row of cache.processedRows || []) {
            yield row;
        }

        for (let i = 0; i < rows.length; i++) {
            if (i < startIdx) continue;
            const row = rows[i];
            const transformed = this._transformWithState(row);
            if (transformed !== null) {
                buffer.push(transformed);
                cache.processedRows.push(transformed);
                if (buffer.length >= this.buffer) {
                    for (const item of buffer) yield item;
                    this._saveCache(cacheFile, cache);
                    buffer.length = 0;
                }
            }
        }
        for (const item of buffer) {
            yield item;
            cache.processedRows.push(item);
        }
        cache.transformState = this.state.toDict();
        cache.processedCount = (cache.processedCount || 0) + buffer.length;
        cache.complete = true;
        this._saveCache(cacheFile, cache);
    }

    _transformWithState(row) {
        const transformed = {};
        // Stateful transforms would go here
        return transformed;
    }

    _getCacheFile(filePath) {
        const key = path.basename(path.dirname(filePath)) + '_' + path.basename(filePath) + '_csv';
        const hash = require('crypto').createHash('md5').update(key).digest('hex').substring(0, 8);
        return path.join(this.cacheDir, hash + '_cache.json');
    }

    _loadCache(cacheFile) {
        if (fs.existsSync(cacheFile)) {
            try {
                return JSON.parse(fs.readFileSync(cacheFile, 'utf8'));
            } catch (e) { }
        }
        return { processedRows: [], transformState: {}, processedCount: 0, complete: false };
    }

    _saveCache(cacheFile, cache) {
        if (!fs.existsSync(path.dirname(cacheFile))) {
            fs.mkdirSync(path.dirname(cacheFile), { recursive: true });
        }
        fs.writeFileSync(cacheFile, JSON.stringify(cache, null, 2));
    }
}

module.exports = { DynamicPreprocessor };
'''


def main():
    parser = argparse.ArgumentParser(
        description="Generate DynamicPreprocessor module from sample files.")
    parser.add_argument("module_name", help="Name of the generated module/package")
    parser.add_argument("--output", required=True, help="Output directory")
    parser.add_argument("--sample", required=True, help="Directory containing input.{ext} and output.{ext}")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--python", action="store_true", help="Generate Python module")
    group.add_argument("--javascript", action="store_true", help="Generate JavaScript module")

    args = parser.parse_args()

    sample_dir = Path(args.sample)
    if not sample_dir.exists():
        raise ValueError(f"Sample directory does not exist: {args.sample}")

    sample_parser = SampleParser(args.sample)
    input_rows, output_rows = sample_parser.load_samples()

    if not input_rows:
        raise ValueError("No input rows found in sample")

    inferrer = StatefulInferrer(input_rows, output_rows, sample_parser.extension)
    code_gen = CodeGenerator(args.module_name, inferrer, sample_parser.extension)

    output_path = Path(args.output)
    output_path.mkdir(parents=True, exist_ok=True)

    if args.python:
        files = code_gen.generate_python()
        for rel_path, content in files.items():
            full_path = output_path / rel_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content)
        print(f"Generated Python package at: {output_path}")

    elif args.javascript:
        files = code_gen.generate_javascript()
        for rel_path, content in files.items():
            full_path = output_path / rel_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content)
        print(f"Generated JavaScript module at: {output_path}")


if __name__ == "__main__":
    main()