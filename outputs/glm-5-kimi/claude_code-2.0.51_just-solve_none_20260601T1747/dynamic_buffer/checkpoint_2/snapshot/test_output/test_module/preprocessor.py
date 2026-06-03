"""Auto-generated DynamicPreprocessor module."""

import csv
import json
import os
import hashlib
from collections import deque


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


class _StreamState:
    """Holds state for stateful transforms."""
    def __init__(self):
        self.prefix_sums = {}
        self.prefix_counts = {}
        self.windows = {}
        self.state_values = {}
        self.prev_values = {}
        self.prev_row = None
        self.pending_row = None
        self.row_index = 0

    def to_dict(self):
        return {
            "prefix_sums": dict(self.prefix_sums),
            "prefix_counts": dict(self.prefix_counts),
            "windows": {k: list(v) for k, v in self.windows.items()},
            "state_values": dict(self.state_values),
            "prev_values": dict(self.prev_values),
            "row_index": self.row_index,
        }

    @classmethod
    def from_dict(cls, d):
        s = cls()
        s.prefix_sums = d.get("prefix_sums", {})
        s.prefix_counts = d.get("prefix_counts", {})
        s.windows = {k: deque(v) for k, v in d.get("windows", {}).items()}
        s.state_values = d.get("state_values", {})
        s.prev_values = d.get("prev_values", {})
        s.row_index = d.get("row_index", 0)
        return s


# Stateful transform configuration
_NEIGHBOR_FILTER_TYPE = None

def _transform_row(_parsed, _state):
    """Apply column transformations to a parsed row."""
    _result = {}
    _result['id'] = _parsed['id']
    _result['name'] = _parsed['name']
    return _result


def _keep_row(_parsed):
    """Determine if a row should be kept (per-row condition)."""
    return _parsed['id'] != 2


def _neighbor_keep_row(_parsed, _state):
    """Determine if a row should be kept based on neighbor conditions."""
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
    state_file = None
    resume_from = 0
    _state = _StreamState()

    if cache_dir is not None:
        os.makedirs(cache_dir, exist_ok=True)
        cache_file = os.path.join(cache_dir, f"cache_{cache_key}")
        state_file = cache_file + ".state"
        index_file = cache_file + ".idx"
        if os.path.isfile(index_file):
            with open(index_file, "r") as f:
                resume_from = int(f.read().strip())
        if os.path.isfile(state_file):
            try:
                with open(state_file, "r") as f:
                    _state = _StreamState.from_dict(json.load(f))
            except Exception:
                pass

    row_count = 0
    buffer_count = 0
    _pending_rows = []

    for raw_row in _read_rows(path, ext):
        _parsed = {}
        for _k, _v in raw_row.items():
            _parsed[_k] = _parse_value(_v)

        # Per-row filtering
        if not _keep_row(_parsed):
            _state.row_index += 1
            continue

        # Neighbor-based filtering
        if not _neighbor_keep_row(_parsed, _state):
            _state.row_index += 1
            continue

        _result = _transform_row(_parsed, _state)
        row_count += 1
        buffer_count += 1
        _state.row_index += 1

        if row_count <= resume_from:
            continue

        if cache_dir is not None and state_file is not None:
            with open(state_file, "w") as f:
                json.dump(_state.to_dict(), f)
            with open(cache_file + ".idx", "w") as f:
                f.write(str(row_count))

        yield _result

        if buffer is not None and buffer_count >= buffer:
            buffer_count = 0


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

