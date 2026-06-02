"""Auto-generated DynamicPreprocessor module."""

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
    return str(row.get('active', '')) == 'true'


def _apply_transform(row: Dict[str, Any], row_index: int) -> Optional[Dict[str, Any]]:
    """Apply column transformations to a row."""
    output_row = {}
    output_row['id'] = _infer_value_type(str(row.get('id', '')))
    output_row['name'] = _infer_value_type(str(row.get('name', '')))
    return output_row


class DynamicPreprocessor:
    """Dynamic preprocessor that applies inferred transformations."""

    def __init__(self, buffer: int, cache_dir: Optional[str] = None):
        self.buffer = buffer
        self.cache_dir = cache_dir
        self._format = 'csv'
        self._output_columns = ['id', 'name']

        if cache_dir:
            Path(cache_dir).mkdir(parents=True, exist_ok=True)

    def __call__(self, path: str) -> Iterator[Dict[str, Any]]:
        return self._process(path)

    def _get_cache_path(self, input_path: str, use_index: int = -1) -> str:
        """Get cache file path."""
        key = hashlib.md5(input_path.encode()).hexdigest()
        if use_index >= 0:
            return os.path.join(self.cache_dir, f"{key}_row_{use_index}.json")
        return os.path.join(self.cache_dir, f"{key}_index.txt")

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
                f.write(f"{idx}\n")

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
                reader = csv.DictReader(f, delimiter='\t')
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
