#!/usr/bin/env python3
"""
Dynamic Buffer Code Generator

Generates a DynamicPreprocessor module that can transform data files
based on inferred transformations from sample input/output pairs.
"""

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple, Set, Union
import pandas as pd

# Data loading / saving

SUPPORTED_EXTS = {"csv", "tsv", "jsonl", "json"}


def detect_extension(directory: Path) -> str:
    for f in directory.iterdir():
        if f.is_file() and f.name.startswith(("input.", "output.")):
            return f.suffix.lstrip(".")
    raise ValueError("No supported data files found in sample directory")


def load_data(path: Path) -> pd.DataFrame:
    ext = path.suffix.lstrip(".")
    if ext == "csv":
        return pd.read_csv(path, dtype=str)
    elif ext == "tsv":
        return pd.read_csv(path, sep="\t", dtype=str)
    elif ext == "jsonl":
        records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        return pd.DataFrame(records)
    elif ext == "json":
        data = json.loads(path.read_text())
        if isinstance(data, list):
            return pd.DataFrame(data)
        else:
            raise ValueError(f"Expected JSON array in {path}, got {type(data)}")
    else:
        raise ValueError(f"Unsupported extension: {ext}")


def infer_delimiter(ext: str) -> str:
    if ext == "tsv":
        return "\t"
    return ","


# Transformation inference

@dataclass
class ColumnTransform:
    """Describes how an input column is transformed to an output column."""
    output_col: str          # name of column in output
    input_col: Optional[str] # name of input column (None if constant)
    transform_type: str      # "identity", "rename", "linear", "trim", "lower", "upper", "const", "copy", "split", "prefix", "suffix", "combine"
    a: float = 1.0           # for linear transform: output = a * input + b
    b: float = 0.0
    constant_value: Any = None
    args: tuple = ()         # extra args (e.g., delimiter for split)


@dataclass
class FilterPredicate:
    """A simple predicate used for row filtering."""
    col: str
    op: str           # "==", "!=", "<", "<=", ">", ">="
    value: Any
    is_and: bool = False
    is_or: bool = False
    children: List["FilterPredicate"] = field(default_factory=list)


@dataclass
class InferredTransformation:
    """The complete inferred transformation."""
    keep_cols: Set[str]           # columns present in output
    col_transforms: Dict[str, ColumnTransform]  # output col -> transform
    filter_predicates: List[FilterPredicate]  # row must satisfy all
    original_cols: Set[str]       # columns in input


def try_parse_number(s: str) -> Union[int, float, str]:
    """Try to convert string to int, then float, else return string."""
    if not isinstance(s, str):
        return s
    try:
        return int(s)
    except ValueError:
        try:
            f = float(s)
            # Avoid treating "30.0" as float when it could be int context
            if f == int(f):
                return int(f)
            return f
        except ValueError:
            return s


def values_match(v1, v2, op: str) -> bool:
    v1 = try_parse_number(str(v1) if not isinstance(v1, (int, float, bool, type(None))) else v1)
    v2 = try_parse_number(str(v2) if not isinstance(v2, (int, float, bool, type(None))) else v2)
    # Handle None
    if v1 is None or v2 is None:
        if op == "==":
            return v1 is None and v2 is None
        elif op == "!=":
            return not (v1 is None and v2 is None)
        return False
    # Handle booleans
    if isinstance(v1, bool) or isinstance(v2, bool):
        v1 = bool(v1)
        v2 = bool(v2)
    # Handle numeric comparison
    try:
        if isinstance(v1, (int, float)) and isinstance(v2, (int, float)):
            if op == "==":
                return v1 == v2
            elif op == "!=":
                return v1 != v2
            elif op == "<":
                return v1 < v2
            elif op == "<=":
                return v1 <= v2
            elif op == ">":
                return v1 > v2
            elif op == ">=":
                return v1 >= v2
    except:
        pass
    # String comparison fallback
    if op == "==":
        return str(v1) == str(v2)
    elif op == "!=":
        return str(v1) != str(v2)
    return False


def infer_filter_predicate(input_df: pd.DataFrame, output_df: pd.DataFrame) -> List[FilterPredicate]:
    """
    Infer what filter conditions were applied to go from input to output.
    Returns a list of AND-ed simple predicates.
    For OR conditions within the same column, we use OR child predicates.
    """
    if len(output_df) == 0 and len(input_df) > 0:
        # Everything was filtered out — can't easily infer, return empty
        return []

    if len(output_df) == len(input_df):
        return []  # No filtering

    # Add a temporary index to track original positions
    input_df = input_df.copy()
    input_df['_row_idx'] = range(len(input_df))
    output_df = output_df.copy()
    output_df['_row_idx'] = range(len(output_df))

    # Find columns present in both dataframes
    common_cols = set(input_df.columns) & set(output_df.columns)
    common_cols.discard('_row_idx')

    # Match rows by finding output rows in input dataframe
    kept_positions = []
    used_input_indices = set()

    for out_idx, out_row in output_df.iterrows():
        # Find matching input row
        for in_idx, in_row in input_df.iterrows():
            if in_idx in used_input_indices:
                continue
            # Check if rows match on common columns
            matches = True
            for col in common_cols:
                if str(in_row[col]) != str(out_row[col]):
                    matches = False
                    break
            if matches:
                kept_positions.append(in_idx)
                used_input_indices.add(in_idx)
                break

    dropped_positions = [p for p in range(len(input_df)) if p not in kept_positions]

    if not dropped_positions:
        return []  # No filtering

    # Analyze columns
    kept_df = input_df.loc[kept_positions]
    dropped_df = input_df.loc[dropped_positions]

    predicates = []
    for col in common_cols:
        col_vals_kept = set(kept_df[col].dropna().unique()) if kept_df[col].notna().any() else set()
        col_vals_dropped = set(dropped_df[col].dropna().unique()) if dropped_df[col].notna().any() else set()

        if not col_vals_kept or not col_vals_dropped:
            continue  # Column doesn't help differentiate

        only_kept = col_vals_kept - col_vals_dropped
        only_dropped = col_vals_dropped - col_vals_kept

        # Check for range conditions
        try:
            kept_numeric = pd.to_numeric(kept_df[col], errors='coerce').dropna()
            dropped_numeric = pd.to_numeric(dropped_df[col], errors='coerce').dropna()
            if len(kept_numeric) > 0 and len(dropped_numeric) > 0:
                if kept_numeric.min() > dropped_numeric.max():
                    predicates.append(FilterPredicate(col=col, op=">", value=float(dropped_numeric.max())))
                    continue
                elif kept_numeric.max() < dropped_numeric.min():
                    predicates.append(FilterPredicate(col=col, op="<", value=float(dropped_numeric.min())))
                    continue
        except:
            pass

        # For each value in kept-only set, create OR condition
        if len(only_kept) >= 1:
            if len(only_kept) <= 10:
                children = [FilterPredicate(col=col, op="==", value=v) for v in only_kept]
                if len(children) == 1:
                    predicates.append(children[0])
                else:
                    main = children[0]
                    main.children = children[1:]
                    main.is_or = True
                    predicates.append(main)
            else:
                # Many values, use complement (drop values in dropped set)
                for v in only_dropped:
                    predicates.append(FilterPredicate(col=col, op="!=", value=v))

    return predicates


def infer_column_transform(input_val: Any, output_val: Any) -> Tuple[str, ColumnTransform]:
    """
    Given an input value and output value, infer what transform was applied.
    Returns (output_col_name, ColumnTransform).
    """
    input_val = str(input_val) if not isinstance(input_val, (str, int, float, bool, type(None))) else input_val
    output_val = str(output_val) if not isinstance(output_val, (str, int, float, bool, type(None))) else output_val

    # Constant column
    if input_val == "" and output_val is not None:
        return "col", ColumnTransform(output_col="col", input_col=None, transform_type="const", constant_value=output_val)

    # Check for linear transform: try to parse as numbers
    try:
        in_num = float(input_val) if input_val != "" else None
        out_num = float(output_val) if output_val != "" else None
        if in_num is not None and out_num is not None:
            # Solve for a, b: out = a * in + b
            # Use another point to check consistency, but just compute a, b from first row
            # For now, just store identity (we'll infer across rows)
            pass
    except (ValueError, TypeError):
        pass

    # String transformations
    if input_val and output_val:
        s_in = str(input_val).strip()
        s_out = str(output_val)

        if s_in == s_out:
            return "col", ColumnTransform(output_col="col", input_col="col", transform_type="identity")

        # Trim
        if s_out == s_in.strip():
            return "col", ColumnTransform(output_col="col", input_col="col", transform_type="trim")

        # Lowercase
        if s_out == s_in.lower():
            return "col", ColumnTransform(output_col="col", input_col="col", transform_type="lower")

        # Uppercase
        if s_out == s_in.upper():
            return "col", ColumnTransform(output_col="col", input_col="col", transform_type="upper")

        # Prefix check
        for prefix_len in range(1, min(len(s_out), len(s_in)) + 1):
            if s_out.startswith(s_in[:prefix_len]):
                suffix = s_out[prefix_len:]
                if suffix and not s_out.endswith(suffix):
                    # This is a prefix transform
                    return "col", ColumnTransform(output_col="col", input_col="col", transform_type="prefix", args=(s_in[:prefix_len],))

    return "col", ColumnTransform(output_col="col", input_col="col", transform_type="identity")


def infer_transformation(input_df: pd.DataFrame, output_df: pd.DataFrame) -> InferredTransformation:
    """
    Infer the complete transformation from input to output.
    """
    input_cols = set(input_df.columns.tolist())
    output_cols = set(output_df.columns.tolist())

    # Determine dropped columns
    dropped_input_cols = input_cols - output_cols

    # Check for renamed columns
    renamed = {}  # output_name -> input_name
    unchanged = set()

    for out_col in output_cols:
        if out_col in input_cols:
            unchanged.add(out_col)
        else:
            # Check if there's a column in input that has same values (except possibly transformed)
            for in_col in input_cols:
                if in_col not in output_cols and in_col not in renamed and in_col not in dropped_input_cols:
                    # Compare values
                    in_vals = input_df[in_col].astype(str).tolist()
                    out_vals = output_df[out_col].astype(str).tolist()
                    if len(in_vals) == len(out_vals):
                        # Check if values match (with possible transform)
                        all_match = True
                        for iv, ov in zip(in_vals, out_vals):
                            if iv.strip() == ov.strip() or iv == ov:
                                continue
                            all_match = False
                            break
                        if all_match:
                            renamed[out_col] = in_col
                            break

    input_cols_used = input_cols - dropped_input_cols - set(renamed.values())

    # Build column transforms
    col_transforms: Dict[str, ColumnTransform] = {}

    for out_col in output_cols:
        if out_col in renamed:
            in_col = renamed[out_col]
            # Check if there's a simple transform
            col_transforms[out_col] = ColumnTransform(
                output_col=out_col,
                input_col=in_col,
                transform_type="rename"
            )
        elif out_col in input_cols:
            in_col = out_col
            col_transforms[out_col] = ColumnTransform(
                output_col=out_col,
                input_col=in_col,
                transform_type="identity"
            )
        else:
            # New column not in input - could be constant or computed
            # Check if all values are the same → constant
            vals = output_df[out_col].dropna().unique()
            if len(vals) == 1:
                col_transforms[out_col] = ColumnTransform(
                    output_col=out_col,
                    input_col=None,
                    transform_type="const",
                    constant_value=vals[0]
                )
            else:
                # Try to figure out what it's based on
                col_transforms[out_col] = ColumnTransform(
                    output_col=out_col,
                    input_col=None,
                    transform_type="copy",
                    constant_value=None
                )

    # Add transforms for renamed/dropped columns
    for in_col in dropped_input_cols:
        if in_col not in renamed.values():
            pass  # Just dropped

    # Infer filter
    filter_preds = infer_filter_predicate(input_df, output_df)

    return InferredTransformation(
        keep_cols=output_cols,
        col_transforms=col_transforms,
        filter_predicates=filter_preds,
        original_cols=input_cols
    )


# Code generation

def python_class_body(trans: InferredTransformation, ext: str) -> str:
    """Generate Python class body for DynamicPreprocessor."""

    # Header
    body = f'''
import csv
import json
import os
from pathlib import Path
from typing import Iterator, Dict, Any, Optional


class DynamicPreprocessor:
    """Generated preprocessor that infers and applies transformations from sample data."""

    def __init__(self, buffer: int, cache_dir: Optional[str] = None):
        self.buffer = buffer
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _get_cache_path(self, filepath: str) -> Path:
        if not self.cache_dir:
            raise ValueError("cache_dir not set")
        h = hashlib.sha256(filepath.encode()).hexdigest()[:16]
        return self.cache_dir / ("cache_" + h + ".json")

    def _apply_transformation(self, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Apply the inferred transformation to a single row. Returns None if row should be filtered."""
'''

    # Add filter logic - restructure as early-return pattern
    if trans.filter_predicates:
        body += '        # Filter predicates - row is kept only if all conditions pass\n'
        for pred in trans.filter_predicates:
            val = json.dumps(pred.value)
            op_map = {
                "==": "==",
                "!=": "!=",
                "<": "<",
                "<=": "<=",
                ">": ">",
                ">=": ">="
            }
            py_op = op_map.get(pred.op, pred.op)
            if pred.children:
                # OR predicate: at least one must match
                body += f'        # Check if row matches at least one allowed value for {pred.col}\n'
                body += '        passed = False\n'
                body += f'        if row.get("{pred.col}", "") {py_op} {val}:\n'
                body += '            passed = True\n'
                for child in pred.children:
                    cval = json.dumps(child.value)
                    body += f'        if not passed and row.get("{pred.col}", "") {py_op} {cval}:\n'
                    body += '            passed = True\n'
                body += '        if not passed:\n'
                body += '            return None\n'
            else:
                # Simple predicate
                body += f'        if not (row.get("{pred.col}", "") {py_op} {val}):\n'
                body += '            return None\n'
        body += '\n'

    # Add column transformation
    body += '        # Apply column transformations\n'
    body += '        out_row = {{}}\n'
    for out_col, t in trans.col_transforms.items():
        if t.transform_type == "identity":
            body += f'        out_row["{out_col}"] = row.get("{t.input_col}", "")\n'
        elif t.transform_type == "rename":
            body += f'        out_row["{out_col}"] = row.get("{t.input_col}", "")\n'
        elif t.transform_type == "const":
            v = json.dumps(t.constant_value)
            body += f'        out_row["{out_col}"] = {v}\n'
        elif t.transform_type == "trim":
            body += f'        out_row["{out_col}"] = str(row.get("{t.input_col}", "")).strip()\n'
        elif t.transform_type == "lower":
            body += f'        out_row["{out_col}"] = str(row.get("{t.input_col}", "")).lower()\n'
        elif t.transform_type == "upper":
            body += f'        out_row["{out_col}"] = str(row.get("{t.input_col}", "")).upper()\n'
        elif t.transform_type == "linear":
            body += f'        try:\n'
            body += f'            val = float(row.get("{t.input_col}", 0))\n'
            body += f'            out_row["{out_col}"] = {t.a} * val + {t.b}\n'
            body += f'            if out_row["{out_col}"] == int(out_row["{out_col}"]):\n'
            body += f'                out_row["{out_col}"] = int(out_row["{out_col}"])\n'
            body += f'        except:\n'
            body += f'            out_row["{out_col}"] = row.get("{t.input_col}", "")\n'
        else:
            body += f'        out_row["{out_col}"] = row.get("{t.input_col}", "")\n'

    body += '        return out_row\n\n'

    # Add streaming processing
    body += f'''    def __call__(self, path: str) -> Iterator[Dict[str, Any]]:
        """Process a file and yield transformed rows."""
        path = Path(path)
        cache_path = self._get_cache_path(str(path)) if self.cache_dir else None

        # Check cache
        cache_state = {{}}
        processed_up_to = 0
        if cache_path and cache_path.exists():
            with open(cache_path, 'r') as f:
                cache_state = json.load(f)
            processed_up_to = cache_state.get("processed_up_to", 0)

        buffer: List[Dict[str, Any]] = []

        if path.suffix.lstrip(".") == "json":
            # JSON array format
            with open(path, 'r', encoding='utf-8') as f:
                all_rows = json.load(f)
            rows = all_rows
        else:
            rows = self._read_rows(path)

        total = sum(1 for _ in rows) if hasattr(rows, '__len__') else None
'''

    if ext == "json":
        body += f'''
        # For JSON, read all and process
        with open(path, 'r', encoding='utf-8') as f:
            all_rows = json.load(f)

        for i, row in enumerate(all_rows):
            if i < processed_up_to:
                # Already processed, yield from cache
                cached = cache_state.get("rows", {{}}).get(str(i))
                if cached:
                    yield cached
                continue

            result = self._apply_transformation(row)
            if result is not None:
                buffer.append(result)
                if len(buffer) >= self.buffer:
                    for buf_row in buffer:
                        yield buf_row
                    buffer = []

        # Yield remaining
        for row in buffer:
            yield row

        # Update cache
        if cache_path:
            cache_state["processed_up_to"] = len(all_rows)
            with open(cache_path, 'w') as f:
                json.dump(cache_state, f)
'''
    else:
        body += f'''
        row_iter = self._read_rows(path)

        for i, row in enumerate(row_iter):
            if i < processed_up_to:
                # Already processed, yield from cache
                cached = cache_state.get("rows", {{}}).get(str(i))
                if cached:
                    yield cached
                continue

            result = self._apply_transformation(row)
            if result is not None:
                buffer.append(result)
                if len(buffer) >= self.buffer:
                    for buf_row in buffer:
                        yield buf_row
                    buffer = []

        # Yield remaining
        for row in buffer:
            yield row

        # Update cache
        if cache_path:
            if "rows" not in cache_state:
                cache_state["rows"] = {{}}
            # Cache the transformed rows we just generated
            with open(path, 'r', encoding='utf-8') as f:
                reader = self._reader_for_format(path)
                all_input_rows = list(reader)
            for j in range(processed_up_to, len(all_input_rows)):
                row = all_input_rows[j]
                result = self._apply_transformation(row)
                if result is not None:
                    cache_state["rows"][str(j)] = result
            cache_state["processed_up_to"] = len(all_input_rows)
            with open(cache_path, 'w') as f:
                json.dump(cache_state, f)
'''

    # Add _read_rows method
    body += f'''
    def _reader_for_format(self, path: Path):
        """Return appropriate reader for file format."""
        ext = path.suffix.lstrip(".")
        if ext == "csv":
            return csv.DictReader(open(path, "r", encoding="utf-8"))
        elif ext == "tsv":
            return csv.DictReader(open(path, "r", encoding="utf-8"), delimiter="\\t")
        elif ext == "jsonl":
            for line in open(path, "r", encoding="utf-8"):
                if line.strip():
                    yield json.loads(line)
            return
        else:
            raise ValueError(f"Unsupported format: {{ext}}")

    def _read_rows(self, path: Path):
        """Read rows from file."""
        ext = path.suffix.lstrip(".")
        if ext == "json":
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    for row in data:
                        yield row
                else:
                    yield data
        else:
            reader = self._reader_for_format(path)
            for row in reader:
                # Convert numeric values
                converted = {{}}
                for k, v in row.items():
                    if v is None or v == "":
                        converted[k] = None
                    else:
                        # Try to convert to int/float
                        try:
                            if '.' in str(v):
                                converted[k] = float(v)
                            else:
                                converted[k] = int(v)
                        except ValueError:
                            converted[k] = v.strip() if isinstance(v, str) else v
                yield converted
'''

    return body


def generate_python_module(module_name: str, trans: InferredTransformation, ext: str) -> Tuple[str, Dict[str, str]]:
    """Generate Python package files. Returns (init_content, {filename: content})."""

    class_body = python_class_body(trans, ext)

    init_content = f'''"""Generated dynamic preprocessing module: {module_name}"""
from .dynamic_buffer import DynamicPreprocessor

__all__ = ["DynamicPreprocessor"]
'''

    files = {
        "__init__.py": init_content,
        "dynamic_buffer.py": class_body
    }

    return init_content, files


def generate_javascript_module(module_name: str, trans: InferredTransformation, ext: str) -> Dict[str, str]:
    """Generate JavaScript module files."""

    # Build filter logic
    filter_js = ""
    if trans.filter_predicates:
        filter_js = "        // Filter predicates\n"
        filter_js += "        let filterPassed = true;\n"
        for pred in trans.filter_predicates:
            val = json.dumps(pred.value)
            op_map = {
                "==": "===",
                "!=": "!==",
                "<": "<",
                "<=": "<=",
                ">": ">",
                ">=": ">="
            }
            js_op = op_map.get(pred.op, pred.op)
            filter_js += f'        if (row["{pred.col}"] {js_op} {val}) {{\n'
            filter_js += '            filterPassed = false;\n'
            filter_js += '        }\n'
        filter_js += '        if (!filterPassed) return null;\n\n'

    # Build column transformation
    transform_js = "        // Apply column transformations\n"
    transform_js += "        const outRow = {};\n"
    for out_col, t in trans.col_transforms.items():
        if t.transform_type == "identity":
            transform_js += f'        outRow["{out_col}"] = row["{t.input_col}"];\n'
        elif t.transform_type == "rename":
            transform_js += f'        outRow["{out_col}"] = row["{t.input_col}"];\n'
        elif t.transform_type == "const":
            v = json.dumps(t.constant_value)
            transform_js += f'        outRow["{out_col}"] = {v};\n'
        elif t.transform_type == "trim":
            transform_js += f'        outRow["{out_col}"] = String(row["{t.input_col}"] || "").trim();\n'
        elif t.transform_type == "lower":
            transform_js += f'        outRow["{out_col}"] = String(row["{t.input_col}"] || "").toLowerCase();\n'
        elif t.transform_type == "upper":
            transform_js += f'        outRow["{out_col}"] = String(row["{t.input_col}"] || "").toUpperCase();\n'
        elif t.transform_type == "linear":
            transform_js += f'        try {{\n'
            transform_js += f'            const val = parseFloat(row["{t.input_col}"] || 0);\n'
            transform_js += f'            outRow["{out_col}"] = {t.a} * val + {t.b};\n'
            transform_js += f'            if (Number.isInteger(outRow["{out_col}"])) {{\n'
            transform_js += f'                outRow["{out_col}"] = Math.floor(outRow["{out_col}"]);\n'
            transform_js += f'            }}\n'
            transform_js += f'        }} catch(e) {{\n'
            transform_js += f'            outRow["{out_col}"] = row["{t.input_col}\"];\n'
            transform_js += f'        }}\n'
        else:
            transform_js += f'        outRow["{out_col}"] = row["{t.input_col}"];\n'

    transform_js += "        return outRow;\n"

    js_code = f'''const fs = require('fs');
const path = require('path');

class DynamicPreprocessor {{
    constructor({{ buffer, cache_dir = null }}) {{
        this.buffer = buffer;
        this.cache_dir = cache_dir;
        if (this.cache_dir) {{
            if (!fs.existsSync(this.cache_dir)) {{
                fs.mkdirSync(this.cache_dir, {{ recursive: true }});
            }}
        }}
    }}

    _getCachePath(filepath) {{
        if (!this.cache_dir) throw new Error("cache_dir not set");
        const crypto = require('crypto');
        const h = crypto.createHash('sha256').update(filepath).digest('hex').substring(0, 16);
        return path.join(this.cache_dir, `cache_${{h}}.json`);
    }}

    _applyTransformation(row) {{
{filter_js}{transform_js}
    }}

    _readRows(filePath) {{
        const ext = path.extname(filePath).slice(1);
        if (ext === 'json') {{
            const data = JSON.parse(fs.readFileSync(filePath, 'utf8'));
            if (Array.isArray(data)) return data;
            return [data];
        }} else if (ext === 'jsonl') {{
            const lines = fs.readFileSync(filePath, 'utf8').split('\\n').filter(l => l.trim());
            return lines.map(l => JSON.parse(l));
        }} else if (ext === 'csv' || ext === 'tsv') {{
            const content = fs.readFileSync(filePath, 'utf8');
            const delimiter = ext === 'tsv' ? '\\t' : ',';
            const lines = content.split('\\n').filter(l => l.trim());
            if (lines.length === 0) return [];
            const headers = lines[0].split(delimiter);
            return lines.slice(1).map(line => {{
                const values = line.split(delimiter);
                const row = {{}};
                headers.forEach((h, i) => {{
                    row[h] = values[i] !== undefined ? values[i] : '';
                }});
                return row;
            }});
        }}
        throw new Error(`Unsupported format: ${{ext}}`);
    }}

    *[Symbol.iterator]({{
        const self = this;
        return {{
            next() {{
                // This is a simplified iterator - actual implementation uses __call__
                return {{ done: true, value: null }};
            }}
        }};
    }})

    __call__(filePath) {{
        const ext = path.extname(filePath).slice(1);
        const cachePath = this.cache_dir ? this._getCachePath(filePath) : null;

        let cacheState = {{}};
        let processedUpTo = 0;
        if (cachePath && fs.existsSync(cachePath)) {{
            cacheState = JSON.parse(fs.readFileSync(cachePath, 'utf8'));
            processedUpTo = cacheState.processed_up_to || 0;
        }}

        const buffer = [];
        const allRows = this._readRows(filePath);

        const transformRow = (row, idx) => {{
            if (idx < processedUpTo) {{
                const cached = cacheState.rows && cacheState.rows[idx];
                return cached;
            }}
            return this._applyTransformation(row);
        }};

        const generator = function*() {{
            for (let i = 0; i < allRows.length; i++) {{
                const result = transformRow(allRows[i], i);
                if (result !== null) {{
                    if (buffer.length >= self.buffer) {{
                        for (const bufRow of buffer) yield bufRow;
                        buffer.length = 0;
                    }}
                    buffer.push(result);
                }}
            }}
            for (const row of buffer) yield row;
        }};

        const gen = generator();

        const result = {{
            *[Symbol.iterator]() {{
                yield* gen;
            }}
        }};

        // Update cache after full processing
        if (cachePath) {{
            const newRows = {{}};
            for (let i = processedUpTo; i < allRows.length; i++) {{
                const result = transformRow(allRows[i], i);
                if (result !== null) newRows[i] = result;
            }}
            cacheState.rows = {{ ...cacheState.rows, ...newRows }};
            cacheState.processed_up_to = allRows.length;
            fs.writeFileSync(cachePath, JSON.stringify(cacheState));
        }}

        return result;
    }}
}}

// Factory function for compatibility
function DynamicPreprocessorFactory(options) {{
    return new DynamicPreprocessor(options);
}}

module.exports = {{ DynamicPreprocessor, DynamicPreprocessorFactory }};
'''

    return {
        "index.js": f"exports.DynamicPreprocessor = require('./dynamic_buffer').DynamicPreprocessor;",
        "dynamic_buffer.js": js_code
    }


# Main entry point

def main():
    parser = argparse.ArgumentParser(
        description="Generate a DynamicPreprocessor module from sample input/output pairs."
    )
    parser.add_argument("module_name", help="Name of the generated module/package")
    parser.add_argument("--output", required=True, help="Output directory for generated module")
    parser.add_argument("--sample", required=True, help="Directory containing input.{ext} and output.{ext}")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--python", action="store_true", help="Generate Python module")
    group.add_argument("--javascript", action="store_true", help="Generate JavaScript module")

    args = parser.parse_args()

    sample_dir = Path(args.sample)
    output_dir = Path(args.output)
    module_name = args.module_name

    # Validate sample directory
    if not sample_dir.exists():
        print(f"Error: Sample directory '{sample_dir}' does not exist", file=sys.stderr)
        sys.exit(1)

    ext = detect_extension(sample_dir)
    if ext not in SUPPORTED_EXTS:
        print(f"Error: Unsupported extension '.{ext}'", file=sys.stderr)
        sys.exit(1)

    # Load input and output data
    input_path = sample_dir / f"input.{ext}"
    output_path = sample_dir / f"output.{ext}"

    if not input_path.exists():
        print(f"Error: Missing input file: {input_path}", file=sys.stderr)
        sys.exit(1)
    if not output_path.exists():
        print(f"Error: Missing output file: {output_path}", file=sys.stderr)
        sys.exit(1)

    input_df = load_data(input_path)
    output_df = load_data(output_path)

    print(f"Loaded {len(input_df)} input rows with columns: {list(input_df.columns)}")
    print(f"Loaded {len(output_df)} output rows with columns: {list(output_df.columns)}")

    # Infer transformation
    trans = infer_transformation(input_df, output_df)

    print(f"\nInferred transformation:")
    print(f"  Dropped columns: {trans.original_cols - trans.keep_cols}")
    print(f"  Filter predicates: {len(trans.filter_predicates)}")
    for out_col, t in trans.col_transforms.items():
        print(f"  {out_col}: {t.transform_type} <- {t.input_col}")

    # Generate code
    if args.python:
        init_content, files = generate_python_module(module_name, trans, ext)
        module_dir = output_dir / module_name
        module_dir.mkdir(parents=True, exist_ok=True)
        for filename, content in files.items():
            filepath = module_dir / filename
            filepath.write_text(content, encoding="utf-8")
        print(f"\nGenerated Python package at: {module_dir}/")
    else:
        files = generate_javascript_module(module_name, trans, ext)
        # For JS, create the module directory with index.js and dynamic_buffer.js
        # We'll output to a file named <module_name>.js as a single file for simplicity
        # But the spec says --output/<module_name>/... so we need a directory
        module_dir = output_dir / module_name
        module_dir.mkdir(parents=True, exist_ok=True)
        for filename, content in files.items():
            filepath = module_dir / filename
            filepath.write_text(content, encoding="utf-8")
        print(f"\nGenerated JavaScript module at: {module_dir}/")

    print("\nDone! You can now import and use the generated module.")


if __name__ == "__main__":
    main()
