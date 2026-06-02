#!/usr/bin/env python3
"""
Dynamic Buffer Code Generator

Given a sample input/output pair, infers transformations and generates a
DynamicPreprocessor module.
"""

import argparse
import csv
import json
import os
import re
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union


# ==============================================================================
# Data Loading
# ==============================================================================

class DataType(Enum):
    CSV = "csv"
    TSV = "tsv"
    JSONL = "jsonl"
    JSON = "json"


def detect_data_format(filepath: str) -> DataType:
    """Detect the data format based on file extension."""
    ext = Path(filepath).suffix.lower().lstrip(".")
    if ext == "csv":
        return DataType.CSV
    elif ext == "tsv":
        return DataType.TSV
    elif ext == "jsonl":
        return DataType.JSONL
    elif ext == "json":
        return DataType.JSON
    else:
        raise ValueError(f"Unsupported file extension: {ext}")


def load_input_data(filepath: str) -> List[Dict[str, Any]]:
    """Load input data based on file format."""
    data_type = detect_data_format(filepath)

    if data_type == DataType.CSV:
        with open(filepath, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            return list(reader)
    elif data_type == DataType.TSV:
        with open(filepath, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter="\t")
            return list(reader)
    elif data_type == DataType.JSONL:
        rows = []
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return rows
    elif data_type == DataType.JSON:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else [data]


def infer_value_type(value: str) -> Any:
    """Infer the type of a string value and convert it."""
    if value is None:
        return None

    # Try boolean
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False

    # Try int
    try:
        return int(value)
    except ValueError:
        pass

    # Try float
    try:
        return float(value)
    except ValueError:
        pass

    # Keep as string
    return value


# ==============================================================================
# Transformation Inference
# ==============================================================================

@dataclass
class ColumnTransform:
    """Represents a column transformation."""
    input_column: Optional[str]  # None if constant column
    transform_type: str  # "identity", "copy", "drop", "const", "numeric", "string_lower", "string_upper", "string_trim"
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FilterPredicate:
    """Represents a filter predicate."""
    condition: str


def infer_transforms(input_rows: List[Dict[str, Any]], output_rows: List[Dict[str, Any]]) -> Tuple[Dict[str, ColumnTransform], Optional[FilterPredicate]]:
    """Infer transformations from input to output rows."""

    if not input_rows:
        return {}, FilterPredicate("False")  # No rows, always filter

    # Determine which rows were kept (output rows at same positions as input)
    # We need to match output rows to input rows
    output_indices = []
    input_idx = 0

    for out_row in output_rows:
        while input_idx < len(input_rows):
            in_row = input_rows[input_idx]
            # Try to match this row by comparing all fields
            match = True
            for key, val in out_row.items():
                in_val = in_row.get(key)
                # Try to convert both to strings for comparison
                if str(val) != str(in_val) if in_val is not None else val is not None:
                    match = False
                    break

            if match:
                output_indices.append(input_idx)
                input_idx += 1
                break
            input_idx += 1

    # Output columns
    output_columns = set(output_rows[0].keys()) if output_rows else set()
    input_columns = set(input_rows[0].keys()) if input_rows else set()

    # First, infer filtering
    all_indices = set(range(len(input_rows)))
    kept_indices = set(output_indices)
    dropped_indices = all_indices - kept_indices

    # Build filter predicate
    filter_pred = build_filter_predicate(input_rows, kept_indices)

    # Now infer column transformations
    column_transforms = {}

    for out_col in output_columns:
        # Check if column exists in input
        possible_input_cols = [c for c in input_columns if c.lower() == out_col.lower() or c == out_col]

        if not possible_input_cols:
            # Check if it's a constant column
            values = set(row.get(out_col) for row in output_rows)
            if len(values) == 1:
                const_val = list(values)[0]
                column_transforms[out_col] = ColumnTransform(
                    input_column=None,
                    transform_type="const",
                    params={"value": const_val}
                )
            else:
                # It's a computed column, try to infer
                col_transform = infer_computed_column(out_col, input_rows, output_rows, output_indices)
                if col_transform:
                    column_transforms[out_col] = col_transform
                else:
                    # Default: identity with None (drop)
                    column_transforms[out_col] = ColumnTransform(
                        input_column=out_col if out_col in input_columns else None,
                        transform_type="identity" if out_col in input_columns else "drop",
                        params={}
                    )
        else:
            # Column exists in input, check for transformations
            in_col = possible_input_cols[0]
            col_transform = infer_column_transform(in_col, out_col, input_rows, output_rows, output_indices)
            column_transforms[out_col] = col_transform

    # Add columns that are in input but not output (they should be dropped)
    for in_col in input_columns:
        if in_col not in output_columns and in_col not in [ct.input_column for ct in column_transforms.values() if ct.input_column]:
            column_transforms[in_col] = ColumnTransform(
                input_column=in_col,
                transform_type="drop",
                params={}
            )

    return column_transforms, filter_pred


def build_filter_predicate(input_rows: List[Dict[str, Any]], kept_indices: set) -> Optional[FilterPredicate]:
    """Build a filter predicate from kept indices."""

    if kept_indices == set(range(len(input_rows))):
        return None  # No filtering

    # Try to infer simple predicates
    for row_idx in kept_indices:
        if row_idx >= len(input_rows):
            continue
        row = input_rows[row_idx]

        # Check for comparison patterns
        for col, val in row.items():
            # Check if this column has consistent values in kept rows
            kept_values = [input_rows[i].get(col) for i in kept_indices]
            dropped_values = [input_rows[i].get(col) for i in range(len(input_rows)) if i not in kept_indices]

            # Try to find a predicate that matches
            if kept_values and dropped_values:
                # Check for equality pattern
                if all(v == val for v in kept_values) and not any(v == val for v in dropped_values):
                    return FilterPredicate(f"{col} == {repr(val)}")

                # Check for inequality pattern
                if all(v != val for v in kept_values) and any(v == val for v in dropped_values):
                    return FilterPredicate(f"{col} != {repr(val)}")

    # Default: use indices
    return FilterPredicate(f"row_index in {sorted(kept_indices)}")


def infer_column_transform(in_col: str, out_col: str, input_rows: List[Dict[str, Any]],
                          output_rows: List[Dict[str, Any]], kept_indices: List[int]) -> ColumnTransform:
    """Infer transformation for a specific column."""

    # Check if it's just a rename (same values)
    in_values = [input_rows[i].get(in_col) for i in kept_indices]
    out_values = [output_rows[i].get(out_col) for i in range(len(output_rows))] if output_rows else []

    # Check for identity (rename or same name)
    if in_values == out_values:
        return ColumnTransform(
            input_column=in_col,
            transform_type="identity" if in_col == out_col else "copy",
            params={}
        )

    # Check for numeric transform: y = a*x + b
    numeric_transform = infer_numeric_transform(in_values, out_values)
    if numeric_transform:
        a, b = numeric_transform
        return ColumnTransform(
            input_column=in_col,
            transform_type="numeric",
            params={"a": a, "b": b}
        )

    # Check for string transforms
    if all(isinstance(v, str) for v in in_values + out_values):
        # Lowercase
        if all(v.lower() == out_v for v, out_v in zip(in_values, out_values)):
            return ColumnTransform(
                input_column=in_col,
                transform_type="string_lower",
                params={}
            )
        # Uppercase
        if all(v.upper() == out_v for v, out_v in zip(in_values, out_values)):
            return ColumnTransform(
                input_column=in_col,
                transform_type="string_upper",
                params={}
            )
        # Trim
        if all(v.strip() == out_v for v, out_v in zip(in_values, out_values)):
            return ColumnTransform(
                input_column=in_col,
                transform_type="string_trim",
                params={}
            )

    # Default: identity
    return ColumnTransform(
        input_column=in_col,
        transform_type="identity",
        params={}
    )


def infer_computed_column(out_col: str, input_rows: List[Dict[str, Any]],
                         output_rows: List[Dict[str, Any]], kept_indices: List[int]) -> Optional[ColumnTransform]:
    """Infer transformation for a column not present in input."""

    # Check if constant
    values = [output_rows[i].get(out_col) for i in range(len(output_rows))]
    if len(set(values)) == 1:
        return ColumnTransform(
            input_column=None,
            transform_type="const",
            params={"value": values[0]}
        )

    # Check for combination of two columns
    for in_col1 in input_rows[0].keys() if input_rows else []:
        for in_col2 in input_rows[0].keys() if input_rows else []:
            if in_col1 == in_col2:
                continue
            test_values = []
            for i in kept_indices:
                v1 = input_rows[i].get(in_col1, "")
                v2 = input_rows[i].get(in_col2, "")
                test_values.append(str(v1) + str(v2))

            out_vals = [output_rows[i].get(out_col, "") for i in range(len(output_rows))]
            if test_values == out_vals:
                return ColumnTransform(
                    input_column=None,
                    transform_type="string_concat",
                    params={"columns": [in_col1, in_col2]}
                )

    return None


def infer_numeric_transform(in_values: List[Any], out_values: List[Any]) -> Optional[Tuple[float, float]]:
    """Infer a numeric transformation y = a*x + b."""

    # Filter to numeric values
    pairs = []
    for in_val, out_val in zip(in_values, out_values):
        try:
            in_num = float(in_val) if in_val is not None else None
            out_num = float(out_val) if out_val is not None else None
            if in_num is not None and out_num is not None:
                pairs.append((in_num, out_num))
        except (ValueError, TypeError):
            continue

    if len(pairs) < 2:
        return None

    # Solve for a and b using linear regression (or exact solve with 2 points)
    if len(pairs) == 2:
        x1, y1 = pairs[0]
        x2, y2 = pairs[1]

        if x1 == x2:
            return None  # Can't determine a uniquely

        a = (y2 - y1) / (x2 - x1)
        b = y1 - a * x1
        return (a, b)

    # Use least squares for more points
    n = len(pairs)
    sum_x = sum(x for x, y in pairs)
    sum_y = sum(y for x, y in pairs)
    sum_xy = sum(x * y for x, y in pairs)
    sum_xx = sum(x * x for x, y in pairs)

    denom = n * sum_xx - sum_x * sum_x
    if denom == 0:
        return None

    a = (n * sum_xy - sum_x * sum_y) / denom
    b = (sum_y - a * sum_x) / n

    return (a, b)


# ==============================================================================
# Code Generation
# ==============================================================================

class CodeGenerator(ABC):
    """Abstract base class for code generators."""

    @abstractmethod
    def generate(self, module_name: str, data_type: DataType,
                 column_transforms: Dict[str, ColumnTransform],
                 filter_pred: Optional[FilterPredicate]) -> str:
        pass


class PythonCodeGenerator(CodeGenerator):
    """Generates Python DynamicPreprocessor module."""

    def generate(self, module_name: str, data_type: DataType,
                 column_transforms: Dict[str, ColumnTransform],
                 filter_pred: Optional[FilterPredicate]) -> str:

        output_columns = [col for col, transform in column_transforms.items()
                         if transform.transform_type != "drop"]

        # Build transformation lines
        transform_lines = []
        for col, transform in column_transforms.items():
            if transform.transform_type == "drop":
                continue

            line = self._generate_transform_line(col, transform)
            transform_lines.append(line)

        filter_code = self._render_filter_body(filter_pred)

        code = f'''"""Auto-generated DynamicPreprocessor module."""

import csv
import json
import hashlib
import os
import shutil
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Any, Set


def _infer_value_type(value: str) -> Any:
    """Infer the type of a string value and convert it."""
    if value is None or value == "":
        return None

    # Try boolean
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False

    # Try int
    try:
        return int(value)
    except ValueError:
        pass

    # Try float
    try:
        return float(value)
    except ValueError:
        pass

    # Keep as string
    return value


def _apply_filter(row: Dict[str, Any], row_index: int) -> bool:
    """Apply the inferred filter predicate."""
    {filter_code}


def _apply_transform(row: Dict[str, Any], row_index: int) -> Optional[Dict[str, Any]]:
    """Apply column transformations to a row."""
    output_row = {{}}
{chr(10).join(transform_lines)}
    return output_row


class DynamicPreprocessor:
    """Dynamic preprocessor that applies inferred transformations."""

    def __init__(self, buffer: int, cache_dir: Optional[str] = None):
        self.buffer = buffer
        self.cache_dir = cache_dir
        self._format = '{data_type.value}'
        self._output_columns = {output_columns}

        if cache_dir:
            Path(cache_dir).mkdir(parents=True, exist_ok=True)

    def __call__(self, path: str) -> Iterator[Dict[str, Any]]:
        return self._process(path)

    def _get_cache_path(self, input_path: str, use_index: int = -1) -> str:
        """Get cache file path."""
        key = hashlib.md5(input_path.encode()).hexdigest()
        if use_index >= 0:
            return os.path.join(self.cache_dir, f"{{key}}_row_{{use_index}}.json")
        return os.path.join(self.cache_dir, f"{{key}}_index.txt")

    def _load_from_cache(self, input_path: str) -> Set[int]:
        """Load processed indices from cache."""
        if not self.cache_dir:
            return set()

        cache_path = self._get_cache_path(input_path)
        if not os.path.exists(cache_path):
            return set()

        try:
            with open(cache_path, 'r') as f:
                return set(int(line.strip()) for line in f if line.strip().isdigit())
        except Exception:
            return set()

    def _save_to_cache(self, input_path: str, indices: Set[int]):
        """Save processed indices to cache."""
        if not self.cache_dir:
            return

        cache_path = self._get_cache_path(input_path)
        with open(cache_path, 'w') as f:
            for idx in sorted(indices):
                f.write(f"{{idx}}\\n")

    def _process(self, path: str) -> Iterator[Dict[str, Any]]:
        """Process the input file."""
        fmt = self._format
        output_columns = self._output_columns

        # Check cache
        processed_indices = self._load_from_cache(path) if self.cache_dir else set()

        buffer_rows = []
        buffer_indices = []

        # Helper function to process buffered rows
        def process_buffer():
            results = []
            result_indices = []
            for row, idx in zip(buffer_rows, buffer_indices):
                if _apply_filter(row, idx):
                    result = _apply_transform(row, idx)
                    if result:
                        results.append(result)
                        result_indices.append(idx)
            return results, result_indices

        if fmt == 'csv':
            with open(path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for idx, row in enumerate(reader):
                    if idx in processed_indices:
                        continue

                    buffer_rows.append(row)
                    buffer_indices.append(idx)

                    if len(buffer_rows) >= self.buffer:
                        results, result_indices = process_buffer()
                        for r in results:
                            yield r
                        processed_indices.update(result_indices)
                        buffer_rows = []
                        buffer_indices = []

            # Process remaining
            if buffer_rows:
                results, result_indices = process_buffer()
                for r in results:
                    yield r
                processed_indices.update(result_indices)

        elif fmt == 'tsv':
            with open(path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f, delimiter='\\t')
                for idx, row in enumerate(reader):
                    if idx in processed_indices:
                        continue

                    buffer_rows.append(row)
                    buffer_indices.append(idx)

                    if len(buffer_rows) >= self.buffer:
                        results, result_indices = process_buffer()
                        for r in results:
                            yield r
                        processed_indices.update(result_indices)
                        buffer_rows = []
                        buffer_indices = []

            # Process remaining
            if buffer_rows:
                results, result_indices = process_buffer()
                for r in results:
                    yield r
                processed_indices.update(result_indices)

        elif fmt == 'jsonl':
            with open(path, 'r', encoding='utf-8') as f:
                for idx, line in enumerate(f):
                    if idx in processed_indices:
                        continue

                    line = line.strip()
                    if not line:
                        continue

                    row = json.loads(line)
                    buffer_rows.append(row)
                    buffer_indices.append(idx)

                    if len(buffer_rows) >= self.buffer:
                        results, result_indices = process_buffer()
                        for r in results:
                            yield r
                        processed_indices.update(result_indices)
                        buffer_rows = []
                        buffer_indices = []

            # Process remaining
            if buffer_rows:
                results, result_indices = process_buffer()
                for r in results:
                    yield r
                processed_indices.update(result_indices)

        elif fmt == 'json':
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if not isinstance(data, list):
                    data = [data]

                for idx, row in enumerate(data):
                    if idx in processed_indices:
                        continue

                    buffer_rows.append(row)
                    buffer_indices.append(idx)

                    if len(buffer_rows) >= self.buffer:
                        results, result_indices = process_buffer()
                        for r in results:
                            yield r
                        processed_indices.update(result_indices)
                        buffer_rows = []
                        buffer_indices = []

            # Process remaining
            if buffer_rows:
                results, result_indices = process_buffer()
                for r in results:
                    yield r
                processed_indices.update(result_indices)

        # Save cache
        if self.cache_dir:
            self._save_to_cache(path, processed_indices)


class DynamicPreprocessorBatch(DynamicPreprocessor):
    """Batch processor for convenience."""

    def process_all(self, path: str) -> List[Dict[str, Any]]:
        return list(self(path))


# Backward compatibility
DynamicPreprocessorBatch = DynamicPreprocessor
'''

        return code

    def _generate_transform_line(self, col: str, transform: ColumnTransform) -> str:
        """Generate a single transformation line."""
        indent = "    "
        line = f"{indent}output_row['{col}'] = "

        if transform.transform_type == "identity":
            line += f"_infer_value_type(str(row.get('{col}', '')))"
        elif transform.transform_type == "copy":
            line += f"_infer_value_type(str(row.get('{col}', '')))"
        elif transform.transform_type == "const":
            val = transform.params.get("value", "")
            if val is True:
                val_str = "True"
            elif val is False:
                val_str = "False"
            elif val is None:
                val_str = "None"
            elif isinstance(val, str):
                val_str = f'"{val}"'
            else:
                val_str = str(val)
            line += val_str
        elif transform.transform_type == "numeric":
            a = transform.params.get("a", 1)
            b = transform.params.get("b", 0)
            input_col = transform.input_column
            line += f"(float(row.get('{input_col}', 0)) * {a}) + {b}"
        elif transform.transform_type == "string_lower":
            input_col = transform.input_column
            line += f"str(row.get('{input_col}', '')).lower()"
        elif transform.transform_type == "string_upper":
            input_col = transform.input_column
            line += f"str(row.get('{input_col}', '')).upper()"
        elif transform.transform_type == "string_trim":
            input_col = transform.input_column
            line += f"str(row.get('{input_col}', '')).strip()"
        elif transform.transform_type == "string_concat":
            cols = transform.params.get("columns", [])
            concat_expr = " + ".join(f"str(row.get('{c}', ''))" for c in cols)
            line += concat_expr
        else:
            line += f"_infer_value_type(str(row.get('{col}', '')))"

        return line

    def _render_filter_body(self, filter_pred: Optional[FilterPredicate]) -> str:
        """Render filter predicate body without leading indentation (template handles that)."""
        if filter_pred is None:
            return "return True"

        condition = filter_pred.condition

        # Convert simple predicates
        if "==" in condition:
            parts = condition.split("==", 1)
            left = parts[0].strip()
            right = parts[1].strip()
            col_name = left.strip("'\"")
            if right.isdigit():
                return f"return row.get('{col_name}', '') == {right}"
            else:
                return f"return str(row.get('{col_name}', '')) == {right}"
        elif "!=" in condition:
            parts = condition.split("!=", 1)
            left = parts[0].strip()
            right = parts[1].strip()
            col_name = left.strip("'\"")
            if right.isdigit():
                return f"return row.get('{col_name}', '') != {right}"
            else:
                return f"return str(row.get('{col_name}', '')) != {right}"
        elif "row_index in" in condition:
            # Extract indices
            match = re.search(r'row_index in \[(.*)\]', condition)
            if match:
                indices_str = match.group(1)
                return f"return {indices_str}.__contains__(row_index)"

        return "return True"


class JavaScriptCodeGenerator(CodeGenerator):
    """Generates JavaScript DynamicPreprocessor module."""

    def generate(self, module_name: str, data_type: DataType,
                 column_transforms: Dict[str, ColumnTransform],
                 filter_pred: Optional[FilterPredicate]) -> str:

        output_columns = [col for col, transform in column_transforms.items()
                         if transform.transform_type != "drop"]

        # Build transformation lines
        transform_lines = []
        for col, transform in column_transforms.items():
            if transform.transform_type == "drop":
                continue

            line = self._generate_transform_line_js(col, transform)
            transform_lines.append(line)

        filter_code = self._render_filter_body_js(filter_pred)

        code = f'''// Auto-generated DynamicPreprocessor module

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

function inferValueType(value) {{
    if (value === null || value === undefined || value === "") {{
        return null;
    }}

    // Try boolean
    if (typeof value === 'boolean') {{
        return value;
    }}
    if (typeof value === 'string') {{
        if (value.toLowerCase() === 'true') return true;
        if (value.toLowerCase() === 'false') return false;
    }}

    // Try int
    const intVal = parseInt(value, 10);
    if (!isNaN(intVal) && String(intVal) === String(value).trim()) {{
        return intVal;
    }}

    // Try float
    const floatVal = parseFloat(value);
    if (!isNaN(floatVal)) {{
        return floatVal;
    }}

    // Keep as string
    return String(value);
}}

function applyFilter(row, rowIndex) {{
{filter_code}
}}

function applyTransform(row, rowIndex) {{
    const outputRow = {{}};
{chr(10).join(transform_lines)}
    return outputRow;
}}

class DynamicPreprocessor {{
    constructor(options = {{}}) {{
        this.buffer = options.buffer || 1024;
        this.cacheDir = options.cache_dir || null;
        this.format = '{data_type.value}';
        this.outputColumns = {output_columns};
        this.processedCache = new Map(); // file path -> Set of indices

        if (this.cacheDir) {{
            if (!fs.existsSync(this.cacheDir)) {{
                fs.mkdirSync(this.cacheDir, {{ recursive: true }});
            }}
        }}
    }}

    [Symbol.iterator]() {{
        throw new Error('DynamicPreprocessor must be called with a path');
    }}

    process(path) {{
        return this._process(path);
    }}

    _getCachePath(filePath, index = -1) {{
        const key = crypto.createHash('md5').update(filePath).digest('hex');
        if (index >= 0) {{
            return path.join(this.cacheDir, `${{key}}_row_${{index}}.json`);
        }}
        return path.join(this.cacheDir, `${{key}}_index.txt`);
    }}

    _loadFromCache(filePath) {{
        if (!this.cacheDir) return new Set();

        const cachePath = this._getCachePath(filePath);
        if (!fs.existsSync(cachePath)) return new Set();

        try {{
            const content = fs.readFileSync(cachePath, 'utf-8');
            const indices = new Set();
            for (const line of content.trim().split('\\n')) {{
                const idx = parseInt(line, 10);
                if (!isNaN(idx)) indices.add(idx);
            }}
            return indices;
        }} catch {{
            return new Set();
        }}
    }}

    _saveToCache(filePath, indices) {{
        if (!this.cacheDir) return;

        const cachePath = this._getCachePath(filePath);
        const lines = Array.from(indices).sort((a, b) => a - b).map(idx => String(idx));
        fs.writeFileSync(cachePath, lines.join('\\n'));
    }}

    _process(filePath) {{
        const fmt = this.format;
        const outputColumns = this.outputColumns;
        const buffer = this.buffer;
        const cacheDir = this.cacheDir;

        // Check cache
        let processedIndices = this._loadFromCache(filePath);

        let bufferRows = [];
        let bufferIndices = [];

        // Helper to process buffered rows
        const processBuffer = () => {{
            const results = [];
            const resultIndices = [];
            for (let i = 0; i < bufferRows.length; i++) {{
                const row = bufferRows[i];
                const idx = bufferIndices[i];
                if (applyFilter(row, idx)) {{
                    const result = applyTransform(row, idx);
                    if (result) {{
                        results.push(result);
                        resultIndices.push(idx);
                    }}
                }}
            }}
            return {{ results, resultIndices }};
        }};

        const generator = (function*() {{
'''

        # Format-specific reading
        if data_type == DataType.CSV:
            code += '''            const content = fs.readFileSync(filePath, 'utf-8');
            const lines = content.trim().split('\\n');
            if (lines.length === 0) return;

            const headers = lines[0].split(',');

            for (let lineNum = 1; lineNum < lines.length; lineNum++) {
                const idx = lineNum - 1;
                if (processedIndices.has(idx)) continue;

                const values = lines[lineNum].split(',');
                const row = {};
                for (let i = 0; i < headers.length; i++) {
                    row[headers[i].trim()] = values[i] !== undefined ? values[i].trim() : '';
                }

                bufferRows.push(row);
                bufferIndices.push(idx);

                if (bufferRows.length >= buffer) {
                    const { results, resultIndices } = processBuffer();
                    for (const result of results) {
                        yield result;
                    }
                    for (const idx of resultIndices) {
                        processedIndices.add(idx);
                    }
                    bufferRows = [];
                    bufferIndices = [];
                }
            }

            // Process remaining
            if (bufferRows.length > 0) {
                const { results, resultIndices } = processBuffer();
                for (const result of results) {
                    yield result;
                }
                for (const idx of resultIndices) {
                    processedIndices.add(idx);
                }
            }'''

        elif data_type == DataType.TSV:
            code += '''            const content = fs.readFileSync(filePath, 'utf-8');
            const lines = content.trim().split('\\n');
            if (lines.length === 0) return;

            const headers = lines[0].split('\\t');

            for (let lineNum = 1; lineNum < lines.length; lineNum++) {
                const idx = lineNum - 1;
                if (processedIndices.has(idx)) continue;

                const values = lines[lineNum].split('\\t');
                const row = {};
                for (let i = 0; i < headers.length; i++) {
                    row[headers[i].trim()] = values[i] !== undefined ? values[i].trim() : '';
                }

                bufferRows.push(row);
                bufferIndices.push(idx);

                if (bufferRows.length >= buffer) {
                    const { results, resultIndices } = processBuffer();
                    for (const result of results) {
                        yield result;
                    }
                    for (const idx of resultIndices) {
                        processedIndices.add(idx);
                    }
                    bufferRows = [];
                    bufferIndices = [];
                }
            }

            // Process remaining
            if (bufferRows.length > 0) {
                const { results, resultIndices } = processBuffer();
                for (const result of results) {
                    yield result;
                }
                for (const idx of resultIndices) {
                    processedIndices.add(idx);
                }
            }'''

        elif data_type == DataType.JSONL:
            code += '''            const content = fs.readFileSync(filePath, 'utf-8');
            const lines = content.trim().split('\\n');

            for (let lineNum = 0; lineNum < lines.length; lineNum++) {
                const line = lines[lineNum].trim();
                if (!line) continue;

                const idx = lineNum;
                if (processedIndices.has(idx)) continue;

                let row;
                try {
                    row = JSON.parse(line);
                } catch {
                    continue;
                }

                bufferRows.push(row);
                bufferIndices.push(idx);

                if (bufferRows.length >= buffer) {
                    const { results, resultIndices } = processBuffer();
                    for (const result of results) {
                        yield result;
                    }
                    for (const idx of resultIndices) {
                        processedIndices.add(idx);
                    }
                    bufferRows = [];
                    bufferIndices = [];
                }
            }

            // Process remaining
            if (bufferRows.length > 0) {
                const { results, resultIndices } = processBuffer();
                for (const result of results) {
                    yield result;
                }
                for (const idx of resultIndices) {
                    processedIndices.add(idx);
                }
            }'''

        elif data_type == DataType.JSON:
            code += '''            const content = fs.readFileSync(filePath, 'utf-8');
            let data;
            try {
                data = JSON.parse(content);
            } catch {
                return;
            }

            if (!Array.isArray(data)) {
                data = [data];
            }

            for (let idx = 0; idx < data.length; idx++) {
                if (processedIndices.has(idx)) continue;

                const row = data[idx];

                bufferRows.push(row);
                bufferIndices.push(idx);

                if (bufferRows.length >= buffer) {
                    const { results, resultIndices } = processBuffer();
                    for (const result of results) {
                        yield result;
                    }
                    for (const idx of resultIndices) {
                        processedIndices.add(idx);
                    }
                    bufferRows = [];
                    bufferIndices = [];
                }
            }

            // Process remaining
            if (bufferRows.length > 0) {
                const { results, resultIndices } = processBuffer();
                for (const result of results) {
                    yield result;
                }
                for (const idx of resultIndices) {
                    processedIndices.add(idx);
                }
            }'''

        code += '''
        };

        // Consume generator to populate cache and yield results
        const iter = generator();
        let result = iter.next();
        while (!result.done) {
            const yielded = result.value;
            // Save cache after each yield for resumability
            if (cacheDir) {
                this._saveToCache(filePath, processedIndices);
            }
            result = iter.next(yielded);
        }

        return iter;
    };

    // Make callable like in the spec
    call(path) {
        return this._process(path);
    }

    // Support function call syntax
    static create(options) {
        return new DynamicPreprocessor(options);
    }
}

// Make it work as both class and callable function
function createPreprocessor(options) {
    return new DynamicPreprocessor(options);
}

// Export
module.exports = {
    DynamicPreprocessor,
    createPreprocessor,
    // Allow calling without 'new'
    default: createPreprocessor
};
'''

        return code

    def _generate_transform_line_js(self, col: str, transform: ColumnTransform) -> str:
        """Generate a single transformation line for JavaScript."""
        indent = "    "
        line = f"{indent}outputRow['{col}'] = "

        if transform.transform_type == "identity":
            line += f"inferValueType(row['{col}'])"
        elif transform.transform_type == "copy":
            line += f"inferValueType(row['{col}'])"
        elif transform.transform_type == "const":
            val = transform.params.get("value", "")
            if val is True:
                val_str = "true"
            elif val is False:
                val_str = "false"
            elif val is None:
                val_str = "null"
            elif isinstance(val, str):
                val_str = f"'{val}'"
            else:
                val_str = str(val)
            line += val_str
        elif transform.transform_type == "numeric":
            a = transform.params.get("a", 1)
            b = transform.params.get("b", 0)
            input_col = transform.input_column
            line += f"(Number(row['{input_col}'] || 0) * {a}) + {b}"
        elif transform.transform_type == "string_lower":
            input_col = transform.input_column
            line += f"String(row['{input_col}'] || '').toLowerCase()"
        elif transform.transform_type == "string_upper":
            input_col = transform.input_column
            line += f"String(row['{input_col}'] || '').toUpperCase()"
        elif transform.transform_type == "string_trim":
            input_col = transform.input_column
            line += f"String(row['{input_col}'] || '').trim()"
        elif transform.transform_type == "string_concat":
            cols = transform.params.get("columns", [])
            concat_expr = " + ".join(f"String(row['{c}'] || '')" for c in cols)
            line += concat_expr
        else:
            line += f"inferValueType(row['{col}'])"

        return line + ";"

    def _render_filter_body_js(self, filter_pred: Optional[FilterPredicate]) -> str:
        """Render filter predicate body for JavaScript."""
        if filter_pred is None:
            return "    return true;"

        condition = filter_pred.condition

        # Convert simple predicates
        if "==" in condition:
            parts = condition.split("==", 1)
            left = parts[0].strip()
            right = parts[1].strip()
            col_name = left.strip("'\"")
            if right.isdigit():
                return f"    return row['{col_name}'] == {right};"
            else:
                return f"    return String(row['{col_name}'] || '') == {right};"
        elif "!=" in condition:
            parts = condition.split("!=", 1)
            left = parts[0].strip()
            right = parts[1].strip()
            col_name = left.strip("'\"")
            if right.isdigit():
                return f"    return row['{col_name}'] != {right};"
            else:
                return f"    return String(row['{col_name}'] || '') != {right};"
        elif "row_index in" in condition:
            # Extract indices
            match = re.search(r'row_index in \[(.*)\]', condition)
            if match:
                indices_str = match.group(1)
                return f"    return [{indices_str}].includes(rowIndex);"

        return "    return true;"


# ==============================================================================
# Main CLI
# ==============================================================================

def main():
    """Generate a DynamicPreprocessor module from sample input/output."""
    parser = argparse.ArgumentParser(description='Generate a DynamicPreprocessor module from sample input/output.')
    parser.add_argument('module_name', help='Name of the generated module/package')
    parser.add_argument('--output', required=True, help='Output directory')
    parser.add_argument('--sample', required=True, help='Sample directory containing input.{ext} and output.{ext}')
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument('--python', action='store_true', default=True, help='Generate Python module (default)')
    group.add_argument('--javascript', action='store_true', dest='javascript', help='Generate JavaScript module')

    args = parser.parse_args()

    target_lang = 'javascript' if args.javascript else 'python'

    # Validate sample directory
    sample_path = Path(args.sample)
    if not sample_path.exists():
        print(f"Error: Sample directory '{args.sample}' does not exist", file=sys.stderr)
        sys.exit(1)

    # Find input and output files
    input_file = None
    output_file = None

    for f in sample_path.iterdir():
        if f.is_file():
            if f.name.startswith('input.'):
                input_file = f
            elif f.name.startswith('output.'):
                output_file = f

    if not input_file or not output_file:
        print("Error: Sample directory must contain 'input.{ext}' and 'output.{ext}'", file=sys.stderr)
        sys.exit(1)

    # Validate they have the same extension
    if input_file.suffix != output_file.suffix:
        print("Error: input and output files must have the same extension", file=sys.stderr)
        sys.exit(1)

    # Load sample data
    print(f"Loading input from: {input_file}")
    print(f"Loading output from: {output_file}")

    input_data = load_input_data(str(input_file))
    output_data = load_input_data(str(output_file))

    print(f"Loaded {len(input_data)} input rows")
    print(f"Loaded {len(output_data)} output rows")

    # Infer transformations
    print("Inferring transformations...")
    column_transforms, filter_pred = infer_transforms(input_data, output_data)

    print(f"Found {len(column_transforms)} column transformations")
    if filter_pred:
        print(f"Found filter predicate: {filter_pred.condition}")
    else:
        print("No filtering applied")

    # Determine data format
    data_type = detect_data_format(str(input_file))

    # Generate code
    print(f"Generating {target_lang} module...")

    if target_lang == 'python':
        generator = PythonCodeGenerator()
        code = generator.generate(args.module_name, data_type, column_transforms, filter_pred)

        # Create package structure
        output_path = Path(args.output) / args.module_name
        output_path.mkdir(parents=True, exist_ok=True)

        # Write __init__.py
        init_content = f'''"""Auto-generated package: {args.module_name}"""

from .dynamic_preprocessor import DynamicPreprocessor

__all__ = ['DynamicPreprocessor']
'''

        (output_path / '__init__.py').write_text(init_content, encoding='utf-8')

        # Write main module
        (output_path / 'dynamic_preprocessor.py').write_text(code, encoding='utf-8')

        print(f"Generated Python package at: {output_path}")

    else:  # javascript
        generator = JavaScriptCodeGenerator()
        code = generator.generate(args.module_name, data_type, column_transforms, filter_pred)

        # Create output directory if needed
        output_path = Path(args.output)
        output_path.mkdir(parents=True, exist_ok=True)

        # Write module file
        module_file = output_path / f"{args.module_name}.js"
        module_file.write_text(code, encoding='utf-8')

        print(f"Generated JavaScript module at: {module_file}")

    print("Done!")


if __name__ == '__main__':
    main()
