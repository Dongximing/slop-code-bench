#!/usr/bin/env python3
"""
Dynamic Buffer Generator

Generates a DynamicPreprocessor module by inferring transformations from sample input/output files.
"""

import argparse
import csv
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Set
from collections import defaultdict


SUPPORTED_EXTS = {"csv", "tsv", "jsonl", "json"}


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
        """Load input and output samples."""
        if self.extension in ("csv", "tsv"):
            input_rows = self._load_delimited(self.input_path, self.extension == "tsv")
            output_rows = self._load_delimited(self.output_path, self.extension == "tsv")
        elif self.extension == "jsonl":
            input_rows = self._load_jsonl(self.input_path)
            output_rows = self._load_jsonl(self.output_path)
        else:  # json
            input_rows = self._load_json_array(self.input_path)
            output_rows = self._load_json_array(self.output_path)
        return input_rows, output_rows

    def _load_delimited(self, filepath: Path, is_tsv: bool) -> List[Dict[str, Any]]:
        rows = []
        delimiter = "\t" if is_tsv else ","
        with open(filepath, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter=delimiter)
            for row in reader:
                converted = {k: self._convert_value(v) for k, v in row.items()}
                rows.append(converted)
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

    def _convert_value(self, v: str) -> Any:
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


class TransformationInferrer:
    """Infer transformations from input/output sample pairs."""

    def __init__(self, input_rows: List[Dict], output_rows: List[Dict], ext: str):
        self.input_rows = input_rows
        self.output_rows = output_rows
        self.extension = ext
        self.filter_predicates: List[Tuple[int, bool]] = []  # (row_idx, kept)
        self.output_columns: Set[str] = set()
        self.column_transforms: Dict[str, Dict] = {}  # output_col -> transform info
        self._input_column_names: List[str] = []
        self._infer()

    def _infer(self):
        """Infer all transformations."""
        # Get input column names in order
        if self.input_rows:
            self._input_column_names = list(self.input_rows[0].keys())

        # Get output column names
        for row in self.output_rows:
            self.output_columns.update(row.keys())

        # Step 1: Match input rows to output rows to find which are kept
        self._match_rows()

        # Step 2: Infer filter predicate
        self._infer_filter()

        # Step 3: Infer column transformations
        self._infer_column_transforms()

    def _match_rows(self):
        """Match output rows to input rows to determine which input rows are kept."""
        # Create a mapping for quick lookup
        input_by_tuple = {}
        for i, row in enumerate(self.input_rows):
            key = tuple(sorted(row.items()))
            if key not in input_by_tuple:
                input_by_tuple[key] = []
            input_by_tuple[key].append(i)

        # For each output row, find matching input row
        kept_mask = [False] * len(self.input_rows)
        used_indices = set()

        for out_row in self.output_rows:
            out_key = tuple(sorted(out_row.items()))

            # Try to find direct match
            found = False
            for key, indices in input_by_tuple.items():
                if key == out_key:
                    for idx in indices:
                        if idx not in used_indices:
                            kept_mask[idx] = True
                            used_indices.add(idx)
                            found = True
                            break
                    if found:
                        break

            # If no direct match, try matching on subset of columns
            if not found:
                for i, in_row in enumerate(self.input_rows):
                    if i in used_indices:
                        continue
                    # Check if out_row is compatible (all values match or are transformed)
                    match = True
                    for k, v in out_row.items():
                        if k not in in_row:
                            match = False
                            break
                        in_val = in_row[k]
                        # Allow for type coercion
                        if in_val != v:
                            if not self._values_compatible(in_val, v):
                                match = False
                                break
                    if match:
                        kept_mask[i] = True
                        used_indices.add(i)
                        found = True
                        break

        # Build list of (idx, kept)
        self.filter_predicates = [(i, kept) for i, kept in enumerate(kept_mask)]

    def _values_compatible(self, val1: Any, val2: Any) -> bool:
        """Check if two values are compatible (equal or convertible)."""
        if val1 == val2:
            return True
        # Check numeric conversion
        if isinstance(val1, (int, float)) and isinstance(val2, (int, float)):
            return abs(val1 - val2) < 1e-10
        if isinstance(val1, str) and isinstance(val2, str):
            return val1 == val2
        # Check string vs number
        try:
            n1 = float(val1) if not isinstance(val1, bool) else None
            n2 = float(val2) if not isinstance(val2, bool) else None
            if n1 is not None and n2 is not None:
                return abs(n1 - n2) < 1e-10
        except (ValueError, TypeError):
            pass
        return False

    def _infer_filter(self):
        """Infer which rows are kept based on the row matching."""
        # This is already done in _match_rows via filter_predicates
        pass

    def _infer_column_transforms(self):
        """Infer transformations for each output column."""
        # Get kept input rows and their corresponding output rows
        kept_input_rows = [r for kept, r in zip([p[1] for p in self.filter_predicates], self.input_rows) if kept]
        output_row_iter = iter(self.output_rows)

        # For each output column, determine its source
        for out_col in self.output_columns:
            transform_info = self._analyze_column_transform(out_col, kept_input_rows)
            self.column_transforms[out_col] = transform_info

    def _analyze_column_transform(self, out_col: str, kept_input_rows: List[Dict]) -> Dict:
        """Analyze how an output column is derived from input columns."""
        # Check if it's a constant
        if not kept_input_rows:
            return {"type": "constant", "value": None}

        first_val = kept_input_rows[0].get(out_col)
        is_constant = True
        for row in kept_input_rows[1:]:
            if row.get(out_col) != first_val:
                is_constant = False
                break

        if is_constant:
            return {"type": "constant", "value": first_val}

        # Check if it's a direct column mapping (copy, rename, or transform)
        candidates = []  # (input_col, transform_details)

        input_cols = set()
        for row in self.input_rows:
            input_cols.update(row.keys())

        for in_col in input_cols:
            # Check if out_col values are derived from in_col
            result = self._check_column_source(in_col, out_col, kept_input_rows)
            if result["score"] > 0:
                candidates.append((in_col, result))

        if not candidates:
            # Fallback: try exact match
            for in_col in input_cols:
                if out_col in self.input_rows[0] or True:  # Allow rename
                    match = True
                    for i, in_row in enumerate(self.input_rows):
                        if not self.filter_predicates[i][1]:
                            continue
                        out_row = self.output_rows[i]
                        if in_row.get(in_col) != out_row.get(out_col):
                            match = False
                            break
                    if match:
                        return {"type": "copy", "source": in_col}

        if candidates:
            # Pick best candidate
            candidates.sort(key=lambda x: x[1]["score"], reverse=True)
            best_in_col, best_info = candidates[0]
            return {"type": "transform", "source": best_in_col, "transform": best_info}

        # Default: treat as dropped column
        return {"type": "dropped"}

    def _check_column_source(self, in_col: str, out_col: str, kept_rows: List[Dict]) -> Dict:
        """Check if out_col can be derived from in_col."""
        # Extract values
        in_vals = [r.get(in_col) for r in kept_rows]
        out_vals = [r.get(out_col) for r in kept_rows]

        # Check for exact match
        exact_matches = sum(1 for iv, ov in zip(in_vals, out_vals) if iv == ov)
        if exact_matches == len(out_vals):
            return {"type": "copy", "score": len(out_vals), "details": {}}

        # Check for string transformations
        str_match = self._check_string_transform(in_vals, out_vals)
        if str_match:
            return {"type": "string_transform", "score": len(out_vals), "details": str_match}

        # Check for numeric linear transform
        num_match = self._check_numeric_transform(in_vals, out_vals)
        if num_match:
            return {"type": "numeric_transform", "score": len(out_vals), "details": num_match}

        # Check for prefix/suffix
        prefix_match = self._check_prefix_suffix(in_vals, out_vals)
        if prefix_match:
            return {"type": "prefix_suffix", "score": len(out_vals), "details": prefix_match}

        # Check for splitting
        split_match = self._check_split(in_vals, out_vals)
        if split_match:
            return {"type": "split", "score": len(out_vals), "details": split_match}

        return {"type": "unknown", "score": 0, "details": {}}

    def _check_string_transform(self, in_vals: List, out_vals: List) -> Optional[Dict]:
        """Check for string transformations (lowercase, uppercase, strip)."""
        if not all(isinstance(v, str) for v in in_vals + out_vals):
            return None

        # Check lowercase
        if all(str(iv).lower() == str(ov).lower() and str(iv) != str(ov)
               for iv, ov in zip(in_vals, out_vals) if iv and ov):
            if all(str(iv).lower() == str(ov) for iv, ov in zip(in_vals, out_vals)):
                return {"op": "lowercase"}

        # Check uppercase
        if all(str(iv).upper() == str(ov) for iv, ov in zip(in_vals, out_vals)):
            return {"op": "uppercase"}

        # Check strip
        if all(str(iv).strip() == str(ov) for iv, ov in zip(in_vals, out_vals)):
            return {"op": "strip"}

        return None

    def _check_numeric_transform(self, in_vals: List, out_vals: List) -> Optional[Dict]:
        """Check for numeric linear transform y = a*x + b."""
        numeric_pairs = []
        for iv, ov in zip(in_vals, out_vals):
            if isinstance(iv, (int, float)) and isinstance(ov, (int, float)):
                numeric_pairs.append((iv, ov))

        if len(numeric_pairs) < 2:
            return None

        # Use two points to solve for a and b
        x1, y1 = numeric_pairs[0]
        x2, y2 = numeric_pairs[1]

        if x2 - x1 == 0:
            return None

        a = (y2 - y1) / (x2 - x1)
        b = y1 - a * x1

        # Verify for all pairs
        for x, y in numeric_pairs:
            expected = a * x + b
            if abs(expected - y) > 1e-6:
                return None

        return {"op": "linear", "a": a, "b": b}

    def _check_prefix_suffix(self, in_vals: List, out_vals: List) -> Optional[Dict]:
        """Check for prefix or suffix addition."""
        if not all(isinstance(v, str) for v in in_vals + out_vals):
            return None

        # Check prefix
        common_prefix = None
        for iv, ov in zip(in_vals, out_vals):
            if not iv.startswith(ov) and not ov:
                break
            prefix = ov[:len(ov) - len(iv)] if len(ov) > len(iv) and iv in ov else None
            if prefix is not None:
                if common_prefix is None:
                    common_prefix = prefix
                elif common_prefix != prefix:
                    common_prefix = None
                    break

        if common_prefix:
            return {"op": "prefix", "value": common_prefix}

        # Check suffix
        common_suffix = None
        for iv, ov in zip(in_vals, out_vals):
            if not iv.endswith(ov) and not ov:
                break
            suffix = ov[len(iv):] if len(ov) > len(iv) and iv in ov else None
            if suffix is not None:
                if common_suffix is None:
                    common_suffix = suffix
                elif common_suffix != suffix:
                    common_suffix = None
                    break

        if common_suffix:
            return {"op": "suffix", "value": common_suffix}

        return None

    def _check_split(self, in_vals: List, out_vals: List) -> Optional[Dict]:
        """Check if output is split from input."""
        # This is a simplified check - looking for if output is a substring of input at certain position
        if not all(isinstance(v, str) for v in in_vals + out_vals):
            return None

        # Check if all outputs appear in inputs
        all_in_inputs = all(ov in iv for iv, ov in zip(in_vals, out_vals) if iv and ov)
        if all_in_inputs:
            # Determine position
            positions = []
            for iv, ov in zip(in_vals, out_vals):
                if iv and ov:
                    pos = iv.find(ov)
                    if pos != -1:
                        positions.append(pos)
            if positions and len(set(positions)) == 1:
                return {"op": "extract", "position": positions[0]}

        return None


class CodeGenerator:
    """Generate DynamicPreprocessor code in target language."""

    def __init__(self, module_name: str, inferrer: TransformationInferrer, ext: str):
        self.module_name = module_name
        self.inferrer = inferrer
        self.extension = ext

    def generate_python(self) -> Dict[str, str]:
        """Generate Python package."""
        files = {}
        files[f"{self.module_name}/__init__.py"] = self._python_init()
        files[f"{self.module_name}/{self.module_name}.py"] = self._python_module()
        return files

    def generate_javascript(self) -> Dict[str, str]:
        """Generate JavaScript module."""
        files = {}
        files[f"{self.module_name}.js"] = self._javascript_module()
        return files

    def _python_init(self) -> str:
        return f'''from .{self.module_name} import DynamicPreprocessor

__all__ = ["DynamicPreprocessor"]
'''

    def _python_module(self) -> str:
        """Generate the Python DynamicPreprocessor module."""
        input_cols = self.inferrer._input_column_names
        output_cols = list(self.inferrer.output_columns)

        # Generate filter function
        filter_code = self._python_filter_code()
        col_transform_code = self._python_column_transforms()

        # Generate parser classes
        parser_classes = self._python_parsers()

        template = '''"""
Auto-generated DynamicPreprocessor module for {{module_name}}.

This module provides a streaming preprocessor that:
1. Reads {{extension.upper()}} files with the same schema as the sample
2. Applies the inferred transformation row-by-row
3. Supports caching and resuming
"""

import csv
import json
import hashlib
from pathlib import Path
from typing import Dict, Iterator, Any, Optional

{{parser_classes}}


class DynamicPreprocessor:
    """
    Dynamic preprocessor that applies inferred transformations to streaming data.

    Args:
        buffer: Maximum number of rows to keep in memory at once
        cache_dir: Optional directory for caching processed rows
    """

    def __init__(self, buffer: int, cache_dir: Optional[str] = None):
        self.buffer = buffer
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._parser_map = {
            "csv": CSVParser,
            "tsv": TSVParser,
            "jsonl": JSONLParser,
            "json": JSONParser,
        }
        self._extension = "{{extension}}"

    def __call__(self, path: str) -> Iterator[Dict[str, Any]]:
        """Process a data file and yield transformed rows.

        Args:
            path: Path to the data file

        Yields:
            Transformed row dictionaries
        """
        return self._process(Path(path))

    def _process(self, path: Path) -> Iterator[Dict[str, Any]]:
        """Process a file with optional caching."""
        parser_class = self._parser_map[self._extension]
        parser = parser_class(path)

        if self.cache_dir is None:
            yield from self._process_direct(parser)
        else:
            yield from self._process_with_cache(parser, path)

    def _process_direct(self, parser) -> Iterator[Dict[str, Any]]:
        """Process without caching."""
        buffer = []

        for i, row in enumerate(parser.parse()):
            {{filter_code}}

            transformed = self._transform_row(i, row)
            if transformed is not None:
                buffer.append(transformed)

                if len(buffer) >= self.buffer:
                    for item in buffer:
                        yield item
                    buffer.clear()

        for item in buffer:
            yield item

    def _process_with_cache(self, parser, path: Path) -> Iterator[Dict[str, Any]]:
        """Process with caching for resumability."""
        cache_file = self._get_cache_file(path)
        cache = self._load_cache(cache_file)

        # Determine starting point
        start_idx = len(cache.get("processed_rows", []))

        buffer = []

        # Yield cached rows first
        for row in cache.get("processed_rows", []):
            yield row

        for i, row in enumerate(parser.parse()):
            if i < start_idx:
                continue

            {{filter_code}}

            transformed = self._transform_row(i, row)
            if transformed is not None:
                buffer.append(transformed)
                cache.setdefault("processed_rows", []).append(transformed)

                if len(buffer) >= self.buffer:
                    for item in buffer:
                        yield item
                    self._save_cache(cache_file, cache)
                    buffer.clear()

        for item in buffer:
            yield item
            cache.setdefault("processed_rows", []).append(item)

        # Mark as complete
        cache["complete"] = True
        self._save_cache(cache_file, cache)

    def _transform_row(self, row_idx: int, row: Dict) -> Optional[Dict[str, Any]]:
        """Apply the inferred transformations to a row."""
        {{col_transform_code}}
        return transformed

    def _get_cache_file(self, path: Path) -> Path:
        """Get cache file path for a given input path."""
        key = f"{{path.parent.name}}_{{path.name}}"
        hash_part = hashlib.md5(key.encode()).hexdigest()[:8]
        return self.cache_dir / f"{{hash_part}}_cache.json"

    def _load_cache(self, cache_file: Path) -> Dict:
        """Load cache from file."""
        if cache_file.exists():
            with open(cache_file, "r") as f:
                return json.load(f)
        return {"processed_rows": [], "complete": False}


    def _save_cache(self, cache_file: Path, cache: Dict):
        """Save cache to file."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        with open(cache_file, "w") as f:
            json.dump(cache, f, default=str)

{{filter_method}}
'''

        return template.replace('{{module_name}}', self.module_name)\
                       .replace('{{extension}}', self.extension)\
                       .replace('{{parser_classes}}', parser_classes)\
                       .replace('{{filter_code}}', filter_code)\
                       .replace('{{col_transform_code}}', col_transform_code)\
                       .replace('{{filter_method}}', self._python_filter_method())


    def _python_filter_code(self) -> str:
        """Generate filter code snippet for processing."""
        # For simplicity, generate a filter based on the kept rows indices
        kept_indices = [i for i, kept in self.inferrer.filter_predicates if kept]
        dropped_indices = [i for i, kept in self.inferrer.filter_predicates if not kept]

        if not dropped_indices:
            return "# No filtering applied - all rows kept"

        # Generate a filter condition using row index
        # This is a simplified approach - check if row index is in kept_indices
        return f"# Filter: keep only rows where row index is in kept_indices"

    def _python_filter_method(self) -> str:
        """Generate the filter predicate method."""
        # Build filter based on which rows were kept in sample
        kept_indices = set(i for i, kept in self.inferrer.filter_predicates if kept)
        dropped_indices = [i for i, kept in self.inferrer.filter_predicates if not kept]

        if not dropped_indices:
            return "    def _keep_row(self, row_idx: int, row: Dict) -> bool:\n        return True\n"

        # Generate filter based on sample data patterns
        # Look for common filter patterns in the data
        filter_expr = self._build_filter_expression()

        return f'''    def _keep_row(self, row_idx: int, row: Dict) -> bool:
        # Based on sample: rows dropped at indices {dropped_indices}
        # This is the inferred filter condition
        {filter_expr}
'''

    def _build_filter_expression(self) -> str:
        """Build a filter expression from the sample data."""
        # Analyze what distinguishes kept from dropped rows
        kept_rows = [self.inferrer.input_rows[i] for i, kept in self.inferrer.filter_predicates if kept]
        dropped_rows = [self.inferrer.input_rows[i] for i, kept in self.inferrer.filter_predicates if not kept]

        if not kept_rows or not dropped_rows:
            return "return True"

        # Look for simple column-based filters
        # Common patterns: equality, comparison, etc.

        # Check each column for distinguishing patterns
        col_filters = []

        for col in self.inferrer._input_column_names:
            kept_vals = [r.get(col) for r in kept_rows if col in r]
            dropped_vals = [r.get(col) for r in dropped_rows if col in r]

            if not kept_vals or not dropped_vals:
                continue

            # Check for equality filter
            if len(set(kept_vals)) == 1:
                val = kept_vals[0]
                if not any(dv == val for dv in dropped_vals if dv is not None):
                    col_filters.append(f'row.get("{col}") == {repr(val)}')

            # Check for inequality filter
            if len(set(dropped_vals)) == 1:
                val = dropped_vals[0]
                if not any(kv == val for kv in kept_vals if kv is not None):
                    col_filters.append(f'row.get("{col}") != {repr(val)}')

            # Check for > or < filters (numeric)
            if all(isinstance(v, (int, float)) for v in kept_vals + dropped_vals):
                max_dropped = max(v for v in dropped_vals if v is not None)
                min_kept = min(v for v in kept_vals if v is not None)

                if min_kept > max_dropped:
                    col_filters.append(f'row.get("{col}") > {max_dropped}')

        if col_filters:
            return f"return {' and '.join(col_filters)}"

        return "return row_idx not in {dropped_indices}"

    def _python_column_transforms(self) -> str:
        """Generate column transformation code."""
        lines = ["transformed = {}"]

        for out_col, transform_info in self.inferrer.column_transforms.items():
            if transform_info["type"] == "dropped":
                continue

            if transform_info["type"] == "constant":
                val = transform_info["value"]
                if val is None:
                    lines.append(f'transformed["{out_col}"] = None')
                elif isinstance(val, bool):
                    lines.append(f'transformed["{out_col}"] = {str(val).lower()}')
                elif isinstance(val, (int, float)):
                    lines.append(f'transformed["{out_col}"] = {val}')
                else:
                    lines.append(f'transformed["{out_col}"] = {repr(val)}')

            elif transform_info["type"] == "copy":
                src_col = transform_info["source"]
                lines.append(f'transformed["{out_col}"] = row.get("{src_col}")')

            elif transform_info["type"] == "transform":
                src_col = transform_info["source"]
                transform_details = transform_info["transform"]

                # Check if it's a direct copy (details is empty)
                if transform_details.get("type") == "copy" or not transform_details.get("details"):
                    lines.append(f'transformed["{out_col}"] = row.get("{src_col}")')
                elif transform_details.get("type") == "string_transform":
                    op = transform_details["details"]["op"]
                    if op == "lowercase":
                        lines.append(f'transformed["{out_col}"] = row.get("{src_col}", "").lower()')
                    elif op == "uppercase":
                        lines.append(f'transformed["{out_col}"] = row.get("{src_col}", "").upper()')
                    elif op == "strip":
                        lines.append(f'transformed["{out_col}"] = row.get("{src_col}", "").strip()')
                elif transform_details.get("type") == "numeric_transform":
                    op = transform_details["details"]["op"]
                    a = transform_details["details"]["a"]
                    b = transform_details["details"]["b"]
                    lines.append(f'transformed["{out_col}"] = {a} * (row.get("{src_col}") or 0) + {b}')
                elif transform_details.get("type") == "prefix_suffix":
                    op = transform_details["details"]["op"]
                    val = transform_details["details"]["value"]
                    if op == "prefix":
                        lines.append(f'transformed["{out_col}"] = "{val}" + str(row.get("{src_col}", ""))')
                    elif op == "suffix":
                        lines.append(f'transformed["{out_col}"] = str(row.get("{src_col}", "")) + "{val}"')

        return "\\n".join(lines)

    def _python_parsers(self) -> str:
        """Generate parser class definitions."""
        return '''
class BaseParser:
    """Base class for file parsers."""

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
        """Generate JavaScript DynamicPreprocessor module."""
        input_cols = self.inferrer._input_column_names
        output_cols = list(self.inferrer.output_columns)

        filter_logic = self._javascript_filter_logic()
        transform_logic = self._javascript_transform_logic()

        parser_code = self._javascript_parsers()

        return f'''/**
 * Auto-generated DynamicPreprocessor module for {self.module_name}
 *
 * This module provides a streaming preprocessor that:
 * 1. Reads {self.extension.upper()} files with the same schema as the sample
 * 2. Applies the inferred transformation row-by-row
 * 3. Supports caching and resuming
 */

const fs = require('fs');
const path = require('path');
const csv = require('csv-parser');
{parser_code}

class DynamicPreprocessor {{
    /**
     * Create a DynamicPreprocessor
     * @param {{number}} buffer - Maximum number of rows to keep in memory
     * @param {{string}} [cache_dir] - Optional directory for caching
     */
    constructor(buffer, cache_dir = null) {{
        this.buffer = buffer;
        this.cacheDir = cache_dir ? path.resolve(cache_dir) : null;
        this.parsers = {{
            csv: CSVParser,
            tsv: TSVParser,
            jsonl: JSONLParser,
            json: JSONParser,
        }};
        this.extension = '{self.extension}';
    }}

    /**
     * Process a data file
     * @param {{string}} filePath - Path to the data file
     * @returns {{Iterable}} Iterator of transformed rows
     */
    process(filePath) {{
        const self = this;
        const parserClass = this.parsers[this.extension];
        const parser = new parserClass(filePath);

        return {{
            *[Symbol.iterator]() {{
                const results = parser.parse();

                if (this.cacheDir === null) {{
                    yield* this._processDirect(results);
                }} else {{
                    yield* this._processWithCache(results, filePath);
                }}
            }}
        }};
    }}

    _processDirect(results) {{
        const buffer = [];
        let rowIndex = 0;

        for (const row of results) {{
            {filter_logic}

            const transformed = this._transformRow(rowIndex, row);
            if (transformed !== null) {{
                buffer.push(transformed);

                if (buffer.length >= this.buffer) {{
                    for (const item of buffer) yield item;
                    buffer.length = 0;
                }}
            }}
            rowIndex++;
        }}

        for (const item of buffer) yield item;
    }}

    _processWithCache(results, filePath) {{
        const cacheFile = this._getCacheFile(filePath);
        const cache = this._loadCache(cacheFile);
        const processedRows = cache.processed_rows || [];

        // Yield cached rows first
        for (const row of processedRows) {{
            yield row;
        }}

        const buffer = [];
        let rowIndex = processedRows.length;

        for (const row of results) {{
            if (rowIndex < processedRows.length) {{
                rowIndex++;
                continue;
            }}

            {filter_logic}

            const transformed = this._transformRow(rowIndex, row);
            if (transformed !== null) {{
                buffer.push(transformed);
                processedRows.push(transformed);

                if (buffer.length >= this.buffer) {{
                    for (const item of buffer) yield item;
                    this._saveCache(cacheFile, cache);
                    buffer.length = 0;
                }}
            }}
            rowIndex++;
        }}

        for (const item of buffer) {{
            yield item;
            processedRows.push(item);
        }}

        cache.processed_rows = processedRows;
        cache.complete = true;
        this._saveCache(cacheFile, cache);
    }}

    _transformRow(rowIndex, row) {{
        {transform_logic}
    }}

    _getCacheFile(filePath) {{
        const key = path.basename(path.dirname(filePath)) + '_' + path.basename(filePath);
        const hash = require('crypto').createHash('md5').update(key).digest('hex').substring(0, 8);
        return path.join(this.cacheDir, hash + '_cache.json');
    }}

    _loadCache(cacheFile) {{
        if (fs.existsSync(cacheFile)) {{
            try {{
                return JSON.parse(fs.readFileSync(cacheFile, 'utf8'));
            }} catch (e) {{
                return {{ processed_rows: [], complete: false }};
            }}
        }}
        return {{ processed_rows: [], complete: false }};
    }}

    _saveCache(cacheFile, cache) {{
        if (!fs.existsSync(path.dirname(cacheFile))) {{
            fs.mkdirSync(path.dirname(cacheFile), {{ recursive: true }});
        }}
        fs.writeFileSync(cacheFile, JSON.stringify(cache, null, 2));
    }}
}}

module.exports = {{ DynamicPreprocessor }};
'''

    def _javascript_filter_logic(self) -> str:
        """Generate filter logic for JavaScript."""
        kept_indices = set(i for i, kept in self.inferrer.filter_predicates if kept)
        dropped_indices = [i for i, kept in self.inferrer.filter_predicates if not kept]

        if not dropped_indices:
            return "// No filtering - all rows kept"

        # Generate a filter based on the sample data
        filter_expr = self._build_js_filter_expression()
        return f"if (!this._keepRow(rowIndex, row)) {{ continue; }}"

    def _build_js_filter_expression(self) -> str:
        """Build JavaScript filter expression."""
        kept_rows = [self.inferrer.input_rows[i] for i, kept in self.inferrer.filter_predicates if kept]
        dropped_rows = [self.inferrer.input_rows[i] for i, kept in self.inferrer.filter_predicates if not kept]

        if not kept_rows or not dropped_rows:
            return "return true;"

        col_filters = []

        for col in self.inferrer._input_column_names:
            kept_vals = [r.get(col) for r in kept_rows if col in r]
            dropped_vals = [r.get(col) for r in dropped_rows if col in r]

            if not kept_vals or not dropped_vals:
                continue

            if len(set(kept_vals)) == 1:
                val = kept_vals[0]
                if not any(dv == val for dv in dropped_vals if dv is not None):
                    col_filters.append(f"row['{col}'] === {repr(val)}")

            if len(set(dropped_vals)) == 1:
                val = dropped_vals[0]
                if not any(kv == val for kv in kept_vals if kv is not None):
                    col_filters.append(f"row['{col}'] !== {repr(val)}")

            if all(isinstance(v, (int, float)) for v in kept_vals + dropped_vals):
                max_dropped = max(v for v in dropped_vals if v is not None)
                min_kept = min(v for v in kept_vals if v is not None)

                if min_kept > max_dropped:
                    col_filters.append(f"row['{col}'] > {max_dropped}")

        if col_filters:
            return f"return {' && '.join(col_filters)};"

        dropped_indices = [i for i, kept in self.inferrer.filter_predicates if not kept]
        dropped_str = ', '.join(map(str, dropped_indices))
        return f"return ![{dropped_str}].includes(rowIndex);"

    def _javascript_filter_method(self) -> str:
        """Generate JavaScript filter method."""
        dropped_indices = [i for i, kept in self.inferrer.filter_predicates if not kept]

        if not dropped_indices:
            return "  _keepRow(rowIndex, row) { return true; }\n"

        return f'''  _keepRow(rowIndex, row) {{
    // Dropped rows at indices: [{dropped_indices}]
    const droppedIndices = [{', '.join(map(str, dropped_indices))}];
    return !droppedIndices.includes(rowIndex);
  }}
'''

    def _javascript_transform_logic(self) -> str:
        """Generate transformation logic for JavaScript."""
        lines = ["const transformed = {};"]

        for out_col, transform_info in self.inferrer.column_transforms.items():
            if transform_info["type"] == "dropped":
                continue

            if transform_info["type"] == "constant":
                val = transform_info["value"]
                if val is None:
                    lines.append(f"transformed['{out_col}'] = null;")
                elif isinstance(val, bool):
                    lines.append(f"transformed['{out_col}'] = {str(val).lower()};")
                elif isinstance(val, (int, float)):
                    lines.append(f"transformed['{out_col}'] = {val};")
                else:
                    lines.append(f"transformed['{out_col}'] = '{val}';")

            elif transform_info["type"] == "copy":
                src_col = transform_info["source"]
                lines.append(f"transformed['{out_col}'] = row['{src_col}'];")

            elif transform_info["type"] == "transform":
                src_col = transform_info["source"]
                details = transform_info["transform"]["details"]

                if details["type"] == "copy":
                    lines.append(f"transformed['{out_col}'] = row['{src_col}'];")
                elif details["type"] == "string_transform":
                    op = details["details"]["op"]
                    if op == "lowercase":
                        lines.append(f"transformed['{out_col}'] = (row['{src_col}'] || '').toLowerCase();")
                    elif op == "uppercase":
                        lines.append(f"transformed['{out_col}'] = (row['{src_col}'] || '').toUpperCase();")
                    elif op == "strip":
                        lines.append(f"transformed['{out_col}'] = (row['{src_col}'] || '').trim();")
                elif details["type"] == "numeric_transform":
                    op = details["details"]["op"]
                    a = details["details"]["a"]
                    b = details["details"]["b"]
                    lines.append(f"transformed['{out_col}'] = {a} * (row['{src_col}'] || 0) + {b};")
                elif details["type"] == "prefix_suffix":
                    op = details["details"]["op"]
                    val = details["details"]["value"]
                    if op == "prefix":
                        lines.append(f"transformed['{out_col}'] = '{val}' + (row['{src_col}'] || '');")
                    elif op == "suffix":
                        lines.append(f"transformed['{out_col}'] = (row['{src_col}'] || '') + '{val}';")

        lines.append("return transformed;")
        return "\\n".join(lines)

    def _javascript_parsers(self) -> str:
        """Generate parser code for JavaScript."""
        return '''
const fs = require('fs');
const path = require('path');

class CSVParser {
    constructor(filePath) {
        this.filePath = filePath;
    }

    parse() {
        const results = [];
        fs.createReadStream(this.filePath)
            .pipe(csv())
            .on('data', (data) => {
                results.push(this._convert(data));
            })
            .on('end', () => {});
        // Convert to async iterator
        let i = 0;
        return {
            [Symbol.asyncIterator]() {
                return this;
            },
            next() {
                if (i < results.length) {
                    return { value: results[i++], done: false };
                }
                return { done: true };
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
}

class TSVParser extends CSVParser {
    parse() {
        const results = [];
        const stream = fs.createReadStream(this.filePath);
        let data = '';
        stream.on('data', (chunk) => { data += chunk; });
        stream.on('end', () => {
            const lines = data.split('\\n');
            if (lines.length < 2) return;
            const headers = lines[0].split('\\t');
            for (let i = 1; i < lines.length; i++) {
                if (!lines[i]) continue;
                const values = lines[i].split('\\t');
                const row = {};
                headers.forEach((h, idx) => {
                    row[h] = this._convert(values[idx] || '');
                });
                results.push(row);
            }
        });

        let i = 0;
        return {
            [Symbol.asyncIterator]() { return this; },
            next() {
                if (i < results.length) return { value: results[i++], done: false };
                return { done: true };
            }
        };
    }
}

class JSONLParser {
    constructor(filePath) {
        this.filePath = filePath;
    }

    parse() {
        const content = fs.readFileSync(this.filePath, 'utf8');
        const lines = content.split('\\n').filter(l => l.trim());
        let i = 0;
        return {
            [Symbol.asyncIterator]() { return this; },
            next() {
                if (i < lines.length) {
                    try {
                        return { value: JSON.parse(lines[i++]), done: false };
                    } catch(e) {
                        return this.next();
                    }
                }
                return { done: true };
            }
        };
    }
}

class JSONParser {
    constructor(filePath) {
        this.filePath = filePath;
    }

    parse() {
        const content = fs.readFileSync(this.filePath, 'utf8');
        const data = JSON.parse(content);
        if (!Array.isArray(data)) {
            return { [Symbol.asyncIterator]: function*() {} };
        }
        let i = 0;
        return {
            [Symbol.asyncIterator]() { return this; },
            next() {
                if (i < data.length) return { value: data[i++], done: false };
                return { done: true };
            }
        };
    }
}
'''


def main():
    parser = argparse.ArgumentParser(
        description="Generate DynamicPreprocessor module from sample input/output files.")
    parser.add_argument("module_name", help="Name of the generated module/package")
    parser.add_argument("--output", required=True, help="Output directory")
    parser.add_argument("--sample", required=True, help="Directory containing input.{ext} and output.{ext}")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--python", action="store_true", help="Generate Python module")
    group.add_argument("--javascript", action="store_true", help="Generate JavaScript module")

    args = parser.parse_args()

    # Validate
    sample_dir = Path(args.sample)
    if not sample_dir.exists():
        raise ValueError(f"Sample directory does not exist: {args.sample}")

    # Load samples
    sample_parser = SampleParser(args.sample)
    input_rows, output_rows = sample_parser.load_samples()

    if not input_rows:
        raise ValueError("No input rows found in sample")

    # Infer transformations
    inferrer = TransformationInferrer(input_rows, output_rows, sample_parser.extension)

    # Generate code
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
