"""Cache management for pipeline tasks."""

import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ast_nodes import CacheConfig


@dataclass
class CachedResult:
    """Cached task result."""
    stdout: str
    stderr: str
    exit_code: int
    success: Dict[str, bool]
    output_files: Dict[str, bytes]
    timestamp: float


class CacheManager:
    """Manages task result caching."""

    def __init__(self, cache_location: str):
        self.cache_location = cache_location

    def get_cache_path(self, cache_key: str) -> str:
        return os.path.join(self.cache_location, cache_key[:2], cache_key[2:4], cache_key + ".json")

    def get_data_path(self, cache_key: str) -> str:
        return os.path.join(self.cache_location, cache_key[:2], cache_key[2:4], cache_key + "_data")

    def compute_key(self, task_name: str, params: Dict[str, Any],
                    inputs: List[str], cache_config: CacheConfig) -> str:
        """Compute a cache key for the task."""
        hasher = hashlib.sha256()
        hasher.update(task_name.encode())

        if cache_config.key_include:
            filtered_params = {k: v for k, v in params.items() if k in cache_config.key_include}
        elif cache_config.key_exclude:
            filtered_params = {k: v for k, v in params.items() if k not in cache_config.key_exclude}
        else:
            filtered_params = params

        hasher.update(json.dumps(filtered_params, sort_keys=True).encode())

        if cache_config.strategy == "content":
            for input_path in sorted(inputs):
                self._hash_path(hasher, input_path)

        if cache_config.version:
            hasher.update(cache_config.version.encode())

        return hasher.hexdigest()

    def _hash_path(self, hasher: Any, path: str):
        """Hash a file or directory contents."""
        if os.path.isfile(path):
            with open(path, 'rb') as f:
                hasher.update(f.read())
        elif os.path.isdir(path):
            for root, dirs, files in os.walk(path):
                dirs.sort()
                for fname in sorted(files):
                    fpath = os.path.join(root, fname)
                    try:
                        with open(fpath, 'rb') as f:
                            hasher.update(fpath.encode())
                            hasher.update(f.read())
                    except (IOError, OSError):
                        pass

    def check(self, cache_key: str, cache_config: CacheConfig) -> Optional[CachedResult]:
        """Check if a valid cache entry exists."""
        cache_path = self.get_cache_path(cache_key)
        if not os.path.exists(cache_path):
            return None

        try:
            with open(cache_path, 'r') as f:
                data = json.load(f)

            cached = CachedResult(
                stdout=data['stdout'],
                stderr=data['stderr'],
                exit_code=data['exit_code'],
                success=data['success'],
                output_files={},
                timestamp=data['timestamp']
            )

            ttl_seconds = self._compute_ttl(cache_config)
            if ttl_seconds > 0 and time.time() - cached.timestamp > ttl_seconds:
                return None

            self._load_output_files(cache_key, cached)
            return cached
        except Exception:
            return None

    def _compute_ttl(self, cache_config: CacheConfig) -> int:
        """Compute total TTL in seconds."""
        ttl = 0
        if cache_config.ttl_seconds:
            ttl += cache_config.ttl_seconds
        if cache_config.ttl_minutes:
            ttl += cache_config.ttl_minutes * 60
        if cache_config.ttl_hours:
            ttl += cache_config.ttl_hours * 3600
        if cache_config.ttl_days:
            ttl += cache_config.ttl_days * 86400
        return ttl

    def _load_output_files(self, cache_key: str, cached: CachedResult):
        """Load cached output files."""
        data_path = self.get_data_path(cache_key)
        if os.path.exists(data_path):
            for fname in os.listdir(data_path):
                fpath = os.path.join(data_path, fname)
                if os.path.isfile(fpath):
                    with open(fpath, 'rb') as f:
                        cached.output_files[fname] = f.read()

    def save(self, cache_key: str, stdout: str, stderr: str, exit_code: int,
             success: Dict[str, bool], output_dir: Optional[str]):
        """Save task result to cache."""
        cache_path = self.get_cache_path(cache_key)
        data_path = self.get_data_path(cache_key)

        os.makedirs(os.path.dirname(cache_path), exist_ok=True)

        with open(cache_path, 'w') as f:
            json.dump({
                'stdout': stdout,
                'stderr': stderr,
                'exit_code': exit_code,
                'success': success,
                'timestamp': time.time()
            }, f)

        if output_dir and os.path.exists(output_dir):
            os.makedirs(data_path, exist_ok=True)
            for item in os.listdir(output_dir):
                src = os.path.join(output_dir, item)
                dst = os.path.join(data_path, item)
                if os.path.isfile(src):
                    shutil.copy2(src, dst)
                elif os.path.isdir(src):
                    if os.path.exists(dst):
                        shutil.rmtree(dst)
                    shutil.copytree(src, dst)

    def restore_files(self, cache_key: str, output_dir: Optional[str],
                      output_files: Dict[str, bytes]):
        """Restore cached output files."""
        if not output_dir or not output_files:
            return
        os.makedirs(output_dir, exist_ok=True)
        for fname, content in output_files.items():
            fpath = os.path.join(output_dir, fname)
            parent = os.path.dirname(fpath)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(fpath, 'wb') as f:
                f.write(content)
