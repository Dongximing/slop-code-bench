"""
Dynamic Preprocessor - Streaming data processor with caching support.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, Iterator, Optional
import hashlib


class DynamicPreprocessor:
    """Streaming preprocessor with caching and resuming support."""

    def __init__(self, buffer: int, cache_dir: Optional[str] = None):
        """
        Initialize the preprocessor.

        Args:
            buffer: Maximum number of rows to keep in memory.
            cache_dir: Optional directory for caching state.
        """
        self.buffer = buffer
        self.cache_dir = cache_dir
        self._config = self._get_config()

    def _get_config(self) -> Dict[str, Any]:
        """Return the transformation configuration."""
        return {
                'column_transforms': {
                        'id': {
                                'type': 'linear',
                                'source': 'id',
                                'a': 2.0,
                                'b': 0.0
                        },
                        'value': {
                                'type': 'linear',
                                'source': 'value',
                                'a': 1.0,
                                'b': 5.0
                        },
                        'label': {
                                'type': 'identity',
                                'source': 'label'
                        }
                },
                'filter_conditions': [
                        {
                                'type': 'equality',
                                'column': 'id',
                                'operator': '!=',
                                'value': 4
                        },
                        {
                                'type': 'equality',
                                'column': 'value',
                                'operator': '!=',
                                'value': 40.0
                        },
                        {
                                'type': 'equality',
                                'column': 'label',
                                'operator': '!=',
                                'value': 'delta'
                        }
                ],
                'input_columns': [
                        'id',
                        'value',
                        'label'
                ],
                'output_columns': [
                        'id',
                        'value',
                        'label'
                ],
                'row_mapping': {
                        0: 0,
                        1: 1,
                        2: 2
                }
        }

    def _get_cache_path(self, input_path: str) -> Path:
        """Get cache file path for an input file."""
        if self.cache_dir is None:
            return None
        path_hash = hashlib.md5(os.path.abspath(input_path).encode()).hexdigest()
        cache_dir = Path(self.cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir / f"{path_hash}.json"

    def _load_cache(self, cache_path: Path) -> Dict:
        """Load cache state from file."""
        if cache_path is None or not cache_path.exists():
            return {"processed_rows": 0, "rows": []}
        with open(cache_path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def _save_cache(self, cache_path: Path, state: Dict):
        """Save cache state to file."""
        if cache_path is None:
            return
        with open(cache_path, 'w', encoding='utf-8') as f:
            json.dump(state, f)

    def _parse_value(self, value: str) -> Any:
        """Parse a string value to appropriate Python type."""
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
        """Check if a row passes all filter conditions."""
        if row.get('id') == 4:
            return False
        if row.get('value') == 40.0:
            return False
        if row.get('label') == 'delta':
            return False
        return True

    def _transform_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """Apply transformations to a row."""
        result = {}
        val = row.get('id')
        if isinstance(val, (int, float)):
            result['id'] = 2.0 * val + 0.0
        else:
            result['id'] = None
        val = row.get('value')
        if isinstance(val, (int, float)):
            result['value'] = 1.0 * val + 5.0
        else:
            result['value'] = None
        result['label'] = row.get('label')
        return result

    def _parse_file(self, path: str) -> Iterator[Dict[str, Any]]:
        """Parse input file and yield rows."""
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
            row = {}
            for i, header in enumerate(headers):
                if i < len(values):
                    row[header] = self._parse_value(values[i])
                else:
                    row[header] = None
            yield row

    def __call__(self, path: str) -> Iterator[Dict[str, Any]]:
        """Process a file and yield transformed rows."""
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
