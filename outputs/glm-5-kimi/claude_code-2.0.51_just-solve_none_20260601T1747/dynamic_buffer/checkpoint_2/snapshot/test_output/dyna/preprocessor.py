"""Auto-generated DynamicPreprocessor module."""

import csv
import json
import os
import hashlib


def _parse_value(v):
    """Parse a raw value to a typed Python primitive."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v
    s = str(v)
    try:
        return int(s)
    except (ValueError, TypeError):
        pass
    try:
        return float(s)
    except (ValueError, TypeError):
        pass
    if s.lower() == "true":
        return True
    if s.lower() == "false":
        return False
    if s.lower() == "null" or s == "":
        return None
    return s


def _try_float(v):
    """Try to convert value to float for comparison."""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _transform_row(_parsed):
    """Apply column transformations to a parsed row."""
    _result = {}
    _result['id'] = _parsed['id']
    _result['name'] = _parsed['name']
    return _result


def _keep_row(_parsed):
    return True


_OUTPUT_COLUMNS = ['id', 'name']

_FILE_EXT = 'csv'


class DynamicPreprocessor:
    def __init__(self, buffer, cache_dir=None):
        self.buffer = buffer
        self.cache_dir = cache_dir

    def __call__(self, path):
        return _iterate(path, self.buffer, self.cache_dir)


def _iterate(path, buffer, cache_dir):
    ext = _FILE_EXT
    cache_key = hashlib.sha256(os.path.abspath(path).encode("utf-8")).hexdigest()
    cache_file = None
    resume_from = 0
    if cache_dir is not None:
        os.makedirs(cache_dir, exist_ok=True)
        cache_file = os.path.join(cache_dir, f"cache_{cache_key}")
        index_file = cache_file + ".idx"
        if os.path.isfile(index_file):
            with open(index_file, "r") as f:
                resume_from = int(f.read().strip())
    row_count = 0
    for raw_row in _read_rows(path, ext):
        _parsed = {}
        for _k, _v in raw_row.items():
            _parsed[_k] = _parse_value(_v)
        if not _keep_row(_parsed):
            continue
        _result = _transform_row(_parsed)
        row_count += 1
        if row_count <= resume_from:
            continue
        if cache_dir is not None and cache_file is not None:
            with open(cache_file + ".idx", "w") as f:
                f.write(str(row_count))
        yield _result


def _read_rows(path, ext):
    """Read rows from a file, yielding dicts."""
    if ext == "csv":
        with open(path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                yield dict(row)
    elif ext == "tsv":
        with open(path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f, delimiter="\t")
            for row in reader:
                yield dict(row)
    elif ext == "jsonl":
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    yield json.loads(line)
    elif ext == "json":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            for row in data:
                yield row

