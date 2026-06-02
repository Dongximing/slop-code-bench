"""
Dynamic Preprocessor - Streaming data processor with caching support.
Supports stateful transforms and neighbor-based filtering.
"""

import hashlib
import json
import os
from collections import deque
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional


class DynamicPreprocessor:
    """Streaming preprocessor with caching and resuming support."""

    def __init__(self, buffer: int, cache_dir: Optional[str] = None):
        self.buffer = buffer
        self.cache_dir = cache_dir

    def _get_cache_path(self, input_path: str) -> Optional[Path]:
        if self.cache_dir is None:
            return None
        path_hash = hashlib.md5(os.path.abspath(input_path).encode()).hexdigest()
        cache_dir = Path(self.cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir / f"{path_hash}.json"

    def _load_cache(self, cache_path: Optional[Path]) -> Dict:
        if cache_path is None or not cache_path.exists():
            return {"processed_rows": 0, "rows": [], "state": {}}
        with open(cache_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            # Restore state from cache
            if "state" in data:
                self._restore_state(data["state"])
            return data

    def _save_cache(self, cache_path: Optional[Path], state: Dict):
        if cache_path is None:
            return
        # Include current state in cache
        state["state"] = self._get_current_state()
        with open(cache_path, 'w', encoding='utf-8') as f:
            json.dump(state, f)

    def _should_keep_row(self, row: Dict[str, Any]) -> bool:
        if row.get('id') == 2:
            return False
        if row.get('name') == 'Bob':
            return False
        if row.get('score') == 200:
            return False
        return True

    def _check_neighbor_filter_0(self, current_row: Dict, next_row: Optional[Dict]) -> bool:
        """Check if current row should be dropped based on next row."""
        if next_row is None:
            return True  # Keep if no next row
        if next_row.get('id') == 3:
            return False  # Drop if next row has the condition
        return True

    def _transform_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        result = {}
        result['id'] = row.get('id')
        result['name'] = row.get('name')
        return result

    def _parse_file(self, path: str) -> Iterator[Dict[str, Any]]:
        with open(path, 'r', encoding='utf-8') as f:
            for row in json.load(f):
                yield row

    def __call__(self, path: str) -> Iterator[Dict[str, Any]]:
        cache_path = self._get_cache_path(path)
        cache_state = self._load_cache(cache_path)
        start_row = cache_state.get("processed_rows", 0)
        row_count = 0
        buffer_count = 0
        current_cache_rows = []
        pending_row = None
        pending_input_idx = None
        prev_row = None

        for row in self._parse_file(path):
            if row_count < start_row:
                if pending_row is not None and pending_input_idx == row_count:
                    # This row was pending, now process it with current as next
                    keep = True
                    keep = keep and self._check_neighbor_filter_0(pending_row, row)
                    if keep and self._should_keep_row(pending_row):
                        transformed = self._transform_row(pending_row)
                        if cache_path is not None:
                            current_cache_rows.append(transformed)
                            buffer_count += 1
                            if buffer_count >= self.buffer:
                                cache_state["processed_rows"] = row_count
                                cache_state["rows"] = current_cache_rows
                                self._save_cache(cache_path, cache_state)
                                current_cache_rows = []
                                buffer_count = 0
                        yield transformed
                    pending_row = None
                    pending_input_idx = None
                prev_row = row
                row_count += 1
                continue

            # Check if this is the continuation of a pending row
            if pending_row is not None:
                keep = True
                keep = keep and self._check_neighbor_filter_0(pending_row, row)
                if keep and self._should_keep_row(pending_row):
                    transformed = self._transform_row(pending_row)
                    if cache_path is not None:
                        current_cache_rows.append(transformed)
                        buffer_count += 1
                        if buffer_count >= self.buffer:
                            cache_state["processed_rows"] = row_count
                            cache_state["rows"] = current_cache_rows
                            self._save_cache(cache_path, cache_state)
                            current_cache_rows = []
                            buffer_count = 0
                    yield transformed
                pending_row = None

            # Set this row as pending (need to see next row)
            pending_row = row
            pending_input_idx = row_count
            prev_row = row
            row_count += 1

            transformed = self._transform_row(row)
            if cache_path is not None:
                current_cache_rows.append(transformed)
                buffer_count += 1
                if buffer_count >= self.buffer:
                    cache_state["processed_rows"] = row_count + 1
                    cache_state["rows"] = current_cache_rows
                    self._save_cache(cache_path, cache_state)
                    current_cache_rows = []
                    buffer_count = 0

            row_count += 1
            yield transformed

        # Handle any pending row at end of file
        if pending_row is not None:
            # Check against all neighbor filters with no next row
            keep = True
            keep = keep and self._check_neighbor_filter_0(pending_row, None)
            if keep and self._should_keep_row(pending_row):
                transformed = self._transform_row(pending_row)
                if cache_path is not None:
                    current_cache_rows.append(transformed)
                    cache_state["processed_rows"] = row_count + 1
                    cache_state["rows"] = current_cache_rows
                    self._save_cache(cache_path, cache_state)
                yield transformed

        if cache_path is not None and (buffer_count > 0 or row_count > start_row):
            cache_state["processed_rows"] = row_count
            cache_state["rows"] = current_cache_rows
            self._save_cache(cache_path, cache_state)
