"""
Cache manager for pipeline task caching.
"""

import os
import sys
import json
import hashlib
import glob
import time
import shutil
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, asdict
from .ast_nodes import CacheConfig, CacheTTL


@dataclass
class CachedResult:
    """Represents a cached task result."""
    task_name: str
    params: Dict[str, Any]
    cache_key: str
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    success: Dict[str, bool]
    output: Optional[str]
    timestamp: float
    input_hashes: Dict[str, str]
    output_files: Dict[str, str]  # filename -> content hash


class CacheManager:
    """Manages task result caching."""

    def __init__(self, cache_dir: str, workspace: str):
        self.cache_dir = cache_dir
        self.workspace = workspace
        self._cache: Dict[str, CachedResult] = {}
        self._load_cache_index()

    def _load_cache_index(self):
        """Load existing cache index from disk."""
        if os.path.exists(self.cache_dir):
            # Load cache entries from disk
            for entry in os.listdir(self.cache_dir):
                entry_path = os.path.join(self.cache_dir, entry)
                if os.path.isdir(entry_path):
                    meta_path = os.path.join(entry_path, "meta.json")
                    if os.path.exists(meta_path):
                        try:
                            with open(meta_path, 'r') as f:
                                data = json.load(f)
                            cached = CachedResult(**data)
                            self._cache[cached.cache_key] = cached
                        except (json.JSONDecodeError, KeyError):
                            pass

    def _compute_file_hash(self, filepath: str) -> str:
        """Compute SHA256 hash of a file."""
        sha256 = hashlib.sha256()
        try:
            with open(filepath, 'rb') as f:
                for chunk in iter(lambda: f.read(8192), b''):
                    sha256.update(chunk)
            return sha256.hexdigest()
        except (IOError, OSError):
            return ""

    def _expand_glob(self, pattern: str) -> List[str]:
        """Expand a glob pattern to list of file paths."""
        # Handle relative paths
        if not os.path.isabs(pattern):
            pattern = os.path.join(self.workspace, pattern)

        files = glob.glob(pattern, recursive=True)
        return [f for f in files if os.path.isfile(f)]

    def _compute_content_key(self, task_name: str, params: Dict[str, Any],
                             inputs: List[str], cache_config: CacheConfig) -> str:
        """Compute cache key based on content hashes."""
        hasher = hashlib.sha256()

        # Include task name
        hasher.update(task_name.encode('utf-8'))

        # Include parameters (filtered by key config if specified)
        param_filter = cache_config.key if cache_config and cache_config.key else None
        filtered_params = {}
        if param_filter:
            if param_filter.include:
                filtered_params = {k: v for k, v in params.items() if k in param_filter.include}
            elif param_filter.exclude:
                filtered_params = {k: v for k, v in params.items() if k not in param_filter.exclude}
            else:
                filtered_params = params
        else:
            filtered_params = params

        # Sort params for consistent hashing
        for key in sorted(filtered_params.keys()):
            hasher.update(f"{key}={filtered_params[key]}".encode('utf-8'))

        # Include input file hashes
        if inputs:
            for input_pattern in inputs:
                files = self._expand_glob(input_pattern)
                for filepath in sorted(files):
                    file_hash = self._compute_file_hash(filepath)
                    hasher.update(f"{filepath}:{file_hash}".encode('utf-8'))

        # Include version if specified
        if cache_config and cache_config.version:
            hasher.update(f"version:{cache_config.version}".encode('utf-8'))

        return hasher.hexdigest()

    def _compute_output_hashes(self, output_dir: Optional[str]) -> Dict[str, str]:
        """Compute hashes of all files in output directory."""
        hashes = {}
        if output_dir and os.path.exists(output_dir):
            for root, dirs, files in os.walk(output_dir):
                for filename in files:
                    filepath = os.path.join(root, filename)
                    relpath = os.path.relpath(filepath, output_dir)
                    hashes[relpath] = self._compute_file_hash(filepath)
        return hashes

    def _get_ttl_seconds(self, ttl: CacheTTL) -> int:
        """Convert TTL config to total seconds."""
        total = 0
        if ttl:
            if ttl.seconds:
                total += ttl.seconds
            if ttl.minutes:
                total += ttl.minutes * 60
            if ttl.hours:
                total += ttl.hours * 3600
            if ttl.days:
                total += ttl.days * 86400
        return total

    def _is_cache_stale(self, cached: CachedResult, cache_config: CacheConfig) -> bool:
        """Check if cache is stale based on TTL."""
        if not cache_config or not cache_config.ttl:
            return False

        ttl_seconds = self._get_ttl_seconds(cache_config.ttl)
        if ttl_seconds <= 0:
            return False

        age = time.time() - cached.timestamp
        return age > ttl_seconds

    def compute_cache_key(self, task_name: str, params: Dict[str, Any],
                          inputs: List[str], cache_config: CacheConfig) -> str:
        """Compute the cache key for a task invocation."""
        return self._compute_content_key(task_name, params, inputs, cache_config)

    def check_cache(self, task_name: str, params: Dict[str, Any],
                    inputs: List[str], cache_config: CacheConfig) -> Tuple[bool, Optional[str], Optional[CachedResult]]:
        """
        Check if a valid cache entry exists.
        Returns (is_hit, cache_key, cached_result).
        """
        cache_key = self.compute_cache_key(task_name, params, inputs, cache_config)

        if cache_key not in self._cache:
            return False, cache_key, None

        cached = self._cache[cache_key]

        # Check strategy
        strategy = cache_config.strategy if cache_config else "content"

        if strategy == "always":
            return True, cache_key, cached
        elif strategy == "stale":
            if not self._is_cache_stale(cached, cache_config):
                return True, cache_key, cached
            return False, cache_key, None
        else:  # content
            # Verify input hashes still match
            current_input_hashes = {}
            if inputs:
                for input_pattern in inputs:
                    files = self._expand_glob(input_pattern)
                    for filepath in files:
                        current_input_hashes[filepath] = self._compute_file_hash(filepath)

            if current_input_hashes == cached.input_hashes:
                return True, cache_key, cached
            return False, cache_key, None

    def store_cache(self, task_name: str, params: Dict[str, Any],
                    inputs: List[str], cache_config: CacheConfig,
                    stdout: str, stderr: str, exit_code: int,
                    timed_out: bool, success: Dict[str, bool],
                    output: Optional[str]) -> str:
        """Store a task result in cache. Returns the cache key."""
        cache_key = self.compute_cache_key(task_name, params, inputs, cache_config)

        # Compute input hashes
        input_hashes = {}
        if inputs:
            for input_pattern in inputs:
                files = self._expand_glob(input_pattern)
                for filepath in files:
                    input_hashes[filepath] = self._compute_file_hash(filepath)

        # Compute output hashes
        output_hashes = self._compute_output_hashes(output)

        cached = CachedResult(
            task_name=task_name,
            params=params,
            cache_key=cache_key,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            timed_out=timed_out,
            success=success,
            output=output,
            timestamp=time.time(),
            input_hashes=input_hashes,
            output_files=output_hashes
        )

        self._cache[cache_key] = cached

        # Persist to disk
        cache_entry_dir = os.path.join(self.cache_dir, cache_key[:16])
        os.makedirs(cache_entry_dir, exist_ok=True)

        meta_path = os.path.join(cache_entry_dir, "meta.json")
        with open(meta_path, 'w') as f:
            json.dump(asdict(cached), f, indent=2)

        # Copy output files
        if output and os.path.exists(output):
            output_copy_dir = os.path.join(cache_entry_dir, "output")
            if os.path.exists(output_copy_dir):
                shutil.rmtree(output_copy_dir)
            shutil.copytree(output, output_copy_dir)

        return cache_key

    def restore_output(self, cached: CachedResult, target_dir: str):
        """Restore cached output files to target directory."""
        cache_entry_dir = os.path.join(self.cache_dir, cached.cache_key[:16])
        cached_output_dir = os.path.join(cache_entry_dir, "output")

        if os.path.exists(cached_output_dir):
            if os.path.exists(target_dir):
                shutil.rmtree(target_dir)
            shutil.copytree(cached_output_dir, target_dir)
