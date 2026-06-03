"""
Auto-generated DynamicPreprocessor module for dyna.

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
            reader = csv.DictReader(f, delimiter="\t")
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
        self._extension = "csv"

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
            # Filter: keep only rows where row index is in kept_indices

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

            # Filter: keep only rows where row index is in kept_indices

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
        transformed = {}\ntransformed["name"] = row.get("name")\ntransformed["id"] = row.get("id")
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

    def _keep_row(self, row_idx: int, row: Dict) -> bool:
        # Based on sample: rows dropped at indices [1]
        # This is the inferred filter condition
        return row.get("id") != 2 and row.get("name") != 'Bob' and row.get("age") != 19 and row.get("age") > 19 and row.get("active") == True and row.get("active") != False and row.get("active") > False

