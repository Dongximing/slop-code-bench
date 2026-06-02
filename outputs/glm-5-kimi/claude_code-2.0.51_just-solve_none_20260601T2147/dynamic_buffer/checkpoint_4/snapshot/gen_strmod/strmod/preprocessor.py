"""Dynamic Preprocessor - Streaming data processor with caching support."""

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Iterator, Optional


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
            return {"processed_rows": 0, "rows": []}
        with open(cache_path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def _save_cache(self, cache_path: Optional[Path], state: Dict):
        if cache_path is None:
            return
        with open(cache_path, 'w', encoding='utf-8') as f:
            json.dump(state, f)

    def _parse_value(self, value: str) -> Any:
        if value == '':
            return None
        if value.lower() == 'true':
            return True
        if value.lower() == 'false':
            return False
        try:
            return int(value)
        except ValueError:
            pass
        try:
            return float(value)
        except ValueError:
            pass
        return value

    def _should_keep_row(self, row: Dict[str, Any]) -> bool:
        return True

    def _transform_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        result = {}
        val = row.get('name')
        result['name'] = val.strip() if isinstance(val, str) else val
        val = row.get('code')
        result['code'] = val.upper() if isinstance(val, str) else val
        return result

    def _parse_file(self, path: str) -> Iterator[Dict[str, Any]]:
        with open(path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        if not lines:
            return
        headers = lines[0].strip().split(',')
        for line in lines[1:]:
            line = line.strip()
            if not line:
                continue
            values = line.split(',')
            row = {header: self._parse_value(values[i]) if i < len(values) else None
                   for i, header in enumerate(headers)}
            yield row

    def __call__(self, path: str) -> Iterator[Dict[str, Any]]:
        cache_path = self._get_cache_path(path)
        cache_state = self._load_cache(cache_path)
        start_row = cache_state.get("processed_rows", 0)
        row_count = 0
        buffer_count = 0
        current_cache_rows = []

        for row in self._parse_file(path):
            if row_count < start_row:
                row_count += 1
                continue
            if not self._should_keep_row(row):
                row_count += 1
                cache_state["processed_rows"] = row_count
                self._save_cache(cache_path, cache_state)
                continue

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

        if cache_path is not None and (buffer_count > 0 or row_count > start_row):
            cache_state["processed_rows"] = row_count
            cache_state["rows"] = current_cache_rows
            self._save_cache(cache_path, cache_state)
