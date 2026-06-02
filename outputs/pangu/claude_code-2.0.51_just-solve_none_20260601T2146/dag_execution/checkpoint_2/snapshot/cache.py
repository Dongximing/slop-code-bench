"""
Cache storage and key generation for pipeline tasks.
"""
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
import shutil


class CacheStorage:
    """Manages cache storage for pipeline tasks."""

    def __init__(self, cache_dir: str = ".pipe-cache"):
        self.cache_dir = Path(cache_dir)
        self.metadata_dir = self.cache_dir / "_metadata"
        self.metadata_dir.mkdir(parents=True, exist_ok=True)

    def _get_cache_key_hash(self, cache_key: str) -> str:
        """Generate a hash for the cache key."""
        return hashlib.sha256(cache_key.encode()).hexdigest()[:16]

    def _get_cache_path(self, cache_key_hash: str) -> Path:
        """Get the cache directory path for a key hash."""
        return self.cache_dir / cache_key_hash[:2] / cache_key_hash[2:4] / cache_key_hash[4:]

    def _get_metadata_path(self, cache_key_hash: str) -> Path:
        """Get the metadata file path for a key hash."""
        return self.metadata_dir / f"{cache_key_hash}.json"

    def store(
        self,
        cache_key: str,
        stdout: str,
        stderr: str,
        exit_code: int,
        success: Dict[str, bool],
        output_dir: Optional[Path],
        task_duration: float
    ) -> None:
        """Store task results in cache."""
        cache_key_hash = self._get_cache_key_hash(cache_key)
        cache_path = self._get_cache_path(cache_key_hash)
        metadata_path = self._get_metadata_path(cache_key_hash)

        # Create cache directory
        cache_path.mkdir(parents=True, exist_ok=True)

        # Save stdout and stderr
        (cache_path / "stdout.txt").write_text(stdout)
        (cache_path / "stderr.txt").write_text(stderr)

        # Save metadata
        metadata = {
            "cache_key": cache_key,
            "timestamp": time.time(),
            "exit_code": exit_code,
            "success": success,
            "duration": task_duration
        }
        with open(metadata_path, 'w') as f:
            json.dump(metadata, f)

        # Copy output files if present
        if output_dir and output_dir.exists():
            output_cache_dir = cache_path / "output"
            shutil.copytree(output_dir, output_cache_dir, dirs_exist_ok=True)

    def retrieve(
        self, cache_key: str
    ) -> Optional[Tuple[str, str, int, Dict[str, bool], Optional[str], float]]:
        """
        Retrieve cached results if available and valid.
        Returns (stdout, stderr, exit_code, success, output_dir, duration) or None.
        """
        cache_key_hash = self._get_cache_key_hash(cache_key)
        cache_path = self._get_cache_path(cache_key_hash)
        metadata_path = self._get_metadata_path(cache_key_hash)

        # Check if cache exists
        if not metadata_path.exists():
            return None

        # Load metadata
        try:
            with open(metadata_path, 'r') as f:
                metadata = json.load(f)
        except (json.JSONDecodeError, IOError):
            return None

        # Verify cache key matches
        if metadata.get("cache_key") != cache_key:
            return None

        # Verify cached files exist
        stdout_path = cache_path / "stdout.txt"
        stderr_path = cache_path / "stderr.txt"
        if not stdout_path.exists() or not stderr_path.exists():
            return None

        # Read results
        stdout = stdout_path.read_text()
        stderr = stderr_path.read_text()
        exit_code = metadata.get("exit_code", 0)
        success = metadata.get("success", {})
        duration = metadata.get("duration", 0.0)

        # Check for output directory
        output_cache_dir = cache_path / "output"
        output_dir = str(output_cache_dir) if output_cache_dir.exists() else None

        return (stdout, stderr, exit_code, success, output_dir, duration)


def generate_cache_key(
    task_name: str,
    params: Dict[str, Any],
    cache_config: Any,
    workspace: Path,
    inputs: Optional[List[str]] = None
) -> str:
    """
    Generate a cache key based on task name, parameters, and inputs.

    Args:
        task_name: Name of the task
        params: Resolved parameters for the task
        cache_config: Cache configuration from the task
        workspace: Workspace path
        inputs: List of input file paths

    Returns:
        A string cache key
    """
    key_parts = [f"task:{task_name}"]

    # Add version if specified
    if hasattr(cache_config, 'version') and cache_config.version:
        key_parts.append(f"version:{cache_config.version}")

    # Add parameters to key
    param_keys = set(params.keys())

    # Apply include/exclude filters
    if hasattr(cache_config, 'key') and cache_config.key:
        if cache_config.key.include:
            # Only include specified params
            param_keys = param_keys.intersection(set(cache_config.key.include))
        elif cache_config.key.exclude:
            # Exclude specified params
            param_keys = param_keys.difference(set(cache_config.key.exclude))

    # Sort for consistent key generation
    for key in sorted(param_keys):
        value = params[key]
        # Use repr for consistent representation
        key_parts.append(f"{key}:{repr(value)}")

    # Add input file contents hash if inputs are specified
    if inputs:
        input_hash = _hash_input_files(inputs, workspace)
        key_parts.append(f"inputs:{input_hash}")

    # Create final key
    key_string = "|".join(key_parts)
    return hashlib.sha256(key_string.encode()).hexdigest()


def _hash_input_files(inputs: List[str], workspace: Path) -> str:
    """Generate a hash of input file contents."""
    hasher = hashlib.sha256()
    for input_path_str in sorted(inputs):
        input_path = workspace / input_path_str
        if input_path.exists():
            # Hash file content
            with open(input_path, 'rb') as f:
                hasher.update(f.read())
        else:
            # File doesn't exist - include path in hash
            hasher.update(input_path_str.encode())
    return hasher.hexdigest()[:16]


def check_cache_validity(
    cache_config: Any,
    metadata: Dict[str, Any]
) -> bool:
    """
    Check if cached result is valid based on strategy and TTL.

    Args:
        cache_config: Cache configuration
        metadata: Cached metadata

    Returns:
        True if cache is valid, False otherwise
    """
    strategy = getattr(cache_config, 'strategy', 'content')

    if strategy == 'always':
        return True

    if strategy == 'stale':
        ttl = getattr(cache_config, 'ttl', None)
        if ttl:
            ttl_seconds = _calculate_ttl_seconds(ttl)
            if ttl_seconds:
                timestamp = metadata.get("timestamp", 0)
                if time.time() - timestamp > ttl_seconds:
                    return False
        return True

    # 'content' strategy - validate by checking inputs
    # This would be implemented by comparing input hashes
    return True


def _calculate_ttl_seconds(ttl: Any) -> Optional[int]:
    """Calculate total TTL seconds from CacheTTL object."""
    total = 0
    if hasattr(ttl, 'seconds') and ttl.seconds:
        total += ttl.seconds
    if hasattr(ttl, 'minutes') and ttl.minutes:
        total += ttl.minutes * 60
    if hasattr(ttl, 'hours') and ttl.hours:
        total += ttl.hours * 3600
    if hasattr(ttl, 'days') and ttl.days:
        total += ttl.days * 86400
    return total if total > 0 else None
