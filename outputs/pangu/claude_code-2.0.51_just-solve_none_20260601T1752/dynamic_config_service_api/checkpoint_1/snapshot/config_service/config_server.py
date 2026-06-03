#!/usr/bin/env python3
"""
Configuration Management Service
A REST service for storing JSON configuration objects with immutable versions,
scoping, rollback, and imports/inheritance.
"""

import argparse
import json
import sys
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from typing import Any, Dict, List, Optional, Set, Tuple, Union
from urllib.parse import parse_qs

from flask import Flask

app = Flask(__name__)

# =============================================================================
# Error Handling
# =============================================================================

class ConfigError(Exception):
    """Base exception for configuration service errors."""
    def __init__(self, code: str, message: str, details: Dict = None, status: int = 400):
        self.code = code
        self.message = message
        self.details = details or {}
        self.status = status
        super().__init__(message)


def error_response(code: str, message: str, details: Dict = None, status: int = 400) -> Tuple:
    """Create a standardized error response."""
    body = {
        "error": {
            "code": code,
            "message": message,
            "details": details or {}
        }
    }
    return json.dumps(body, separators=(',', ':'), default=str), status, {
        'Content-Type': 'application/json; charset=utf-8'
    }


# =============================================================================
# Data Models
# =============================================================================

@dataclass(frozen=True)
class Scope:
    """Represents a scope as a flat string-to-string map."""
    items: Tuple[Tuple[str, str], ...]

    def __init__(self, scope_dict: Dict[str, str] = None):
        if scope_dict is None:
            scope_dict = {}
        # Validate scope: flat string-to-string
        for k, v in scope_dict.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise ConfigError("invalid_input", f"Scope keys and values must be strings, got {type(k).__name__}/{type(v).__name__}")
        # Sort items for consistent hashing
        object.__setattr__(self, 'items', tuple(sorted(scope_dict.items())))

    def as_dict(self) -> Dict[str, str]:
        return dict(self.items)

    def as_ordered_dict(self) -> 'OrderedDict[str, str]':
        return OrderedDict(self.items)

    def __hash__(self):
        return hash(self.items)

    def __eq__(self, other):
        if not isinstance(other, Scope):
            return False
        return self.items == other.items

    def copy(self) -> Dict[str, str]:
        return self.as_dict()


@dataclass(frozen=True)
class IncludeRef:
    """Reference to another config version."""
    name: str
    scope: Scope
    version: Optional[int]  # None means use active version

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name:
            raise ConfigError("invalid_input", "Include name must be a non-empty string")
        if not isinstance(self.scope, Scope):
            raise ConfigError("invalid_input", "Include scope must be a scope object")
        if self.version is not None and (not isinstance(self.version, int) or self.version < 1):
            raise ConfigError("invalid_input", "Include version must be a positive integer or null")

    @classmethod
    def from_dict(cls, data: Dict) -> 'IncludeRef':
        """Create IncludeRef from dictionary."""
        if not isinstance(data, dict):
            raise ConfigError("invalid_input", "Include reference must be an object")

        name = data.get('name')
        if not isinstance(name, str) or not name:
            raise ConfigError("invalid_input", "Include reference 'name' must be a non-empty string")

        scope_data = data.get('scope')
        if not isinstance(scope_data, dict):
            raise ConfigError("invalid_input", "Include reference 'scope' must be an object")

        version = data.get('version')
        if version is not None and version is not None:  # Explicit None check
            if version is None:
                version = None
            elif not isinstance(version, int) or version < 1:
                raise ConfigError("invalid_input", "Include version must be a positive integer or null")

        scope = Scope(scope_data)
        return cls(name=name, scope=scope, version=version)

    def to_dict(self) -> Dict:
        """Convert to dictionary for JSON serialization."""
        result = {
            "name": self.name,
            "scope": self.scope.as_dict()
        }
        if self.version is not None:
            result["version"] = self.version
        return result


@dataclass
class ConfigVersion:
    """An immutable version of a configuration."""
    config: Dict[str, Any]
    includes: List[IncludeRef]
    created_at: float = field(default_factory=lambda: datetime.now().timestamp())

    def __post_init__(self):
        if not isinstance(self.config, dict):
            raise ConfigError("invalid_input", "Config must be a JSON object")
        if not isinstance(self.includes, list):
            raise ConfigError("invalid_input", "Includes must be a list")
        for inc in self.includes:
            if not isinstance(inc, IncludeRef):
                raise ConfigError("invalid_input", f"Include must be IncludeRef, got {type(inc)}")

    def to_dict(self, include_includes: bool = True) -> Dict:
        """Convert to dictionary."""
        result = {"config": self.config}
        if include_includes:
            result["includes"] = [inc.to_dict() for inc in self.includes]
        return result


@dataclass
class ConfigEntry:
    """A version entry with metadata."""
    version: int
    config: Dict[str, Any]
    includes: List[IncludeRef]
    active: bool
    created_at: float

    def to_dict(self, include_config: bool = False) -> Dict:
        """Convert to dictionary for API response."""
        result = {
            "version": self.version,
            "active": self.active
        }
        if include_config:
            result["config"] = self.config
            result["includes"] = [inc.to_dict() for inc in self.includes]
        return result


# =============================================================================
# Storage
# =============================================================================

class ConfigStorage:
    """In-memory storage for configurations."""

    MAX_VERSIONS = 10_000
    MAX_INCLUDE_DEPTH = 64

    def __init__(self):
        # Key: (name, scope) -> ConfigStore
        self._stores: Dict[Tuple[str, Scope], 'ConfigStore'] = {}

    def _get_store(self, name: str, scope: Scope) -> 'ConfigStore':
        """Get or create a store for a (name, scope) pair."""
        key = (name, scope)
        if key not in self._stores:
            self._stores[key] = ConfigStore(name, scope)
        return self._stores[key]

    def get_store(self, name: str, scope: Scope) -> Optional['ConfigStore']:
        """Get a store if it exists."""
        return self._stores.get((name, scope))

    def create_version(self, name: str, scope: Scope, config: Dict, includes: List[IncludeRef],
                       inherits_active: bool = False) -> Tuple[int, bool]:
        """Create a new version of a configuration."""
        store = self._get_store(name, scope)
        return store.create_version(config, includes, inherits_active)

    def list_versions(self, name: str, scope: Scope) -> List[Dict]:
        """List all versions for a (name, scope) pair."""
        store = self.get_store(name, scope)
        if store is None:
            return []
        return store.list_versions()

    def get_version(self, name: str, scope: Scope, version: int) -> Optional[ConfigEntry]:
        """Get a specific version."""
        store = self.get_store(name, scope)
        if store is None:
            return None
        return store.get_version(version)

    def get_active(self, name: str, scope: Scope) -> Optional[ConfigEntry]:
        """Get the active version."""
        store = self.get_store(name, scope)
        if store is None:
            return None
        return store.get_active()

    def activate_version(self, name: str, scope: Scope, version: int) -> bool:
        """Activate a specific version. Returns True if successful."""
        store = self.get_store(name, scope)
        if store is None:
            return False
        return store.activate_version(version)


class ConfigStore:
    """Stores all versions for a single (name, scope) pair."""

    def __init__(self, name: str, scope: Scope):
        self.name = name
        self.scope = scope
        self._versions: List[ConfigEntry] = []
        self._version_map: Dict[int, ConfigEntry] = {}
        self._active_version: Optional[int] = None

    def create_version(self, config: Dict, includes: List[IncludeRef],
                       inherits_active: bool = False) -> Tuple[int, bool]:
        """Create a new version, returning (version_number, is_active)."""
        # Handle inherits_active
        if inherits_active and self._active_version is not None:
            active_entry = self._version_map[self._active_version]
            # Merge active config into new config (child overrides parent)
            merged_config = deep_merge({}, active_entry.config, config)
            config = merged_config

        # Generate new version number
        new_version = len(self._versions) + 1

        if new_version > self.MAX_VERSIONS:
            raise ConfigError("conflict", f"Maximum versions ({self.MAX_VERSIONS}) exceeded for ({self.name}, {self.scope.as_dict()})",
                            status=409)

        entry = ConfigEntry(
            version=new_version,
            config=config,
            includes=includes,
            active=True,  # New versions are active by default
            created_at=datetime.now().timestamp()
        )

        # Deactivate previous active version
        if self._active_version is not None and self._version_map[self._active_version] is not None:
            self._version_map[self._active_version].active = False

        self._versions.append(entry)
        self._version_map[new_version] = entry
        self._active_version = new_version

        return new_version, True

    def list_versions(self) -> List[Dict]:
        """List all versions sorted ascending by version number."""
        return [
            {"version": e.version, "active": e.active}
            for e in self._versions
        ]

    def get_version(self, version: int) -> Optional[ConfigEntry]:
        """Get a specific version."""
        return self._version_map.get(version)

    def get_active(self) -> Optional[ConfigEntry]:
        """Get the active version."""
        if self._active_version is None:
            return None
        return self._version_map.get(self._active_version)

    def activate_version(self, version: int) -> bool:
        """Activate a specific version. Returns True if successful."""
        if version not in self._version_map:
            return False

        # Deactivate current active
        if self._active_version is not None and self._version_map[self._active_version] is not None:
            self._version_map[self._active_version].active = False

        # Activate new version
        self._version_map[version].active = True
        self._active_version = version

        return True


# =============================================================================
# Normalization (Canonical JSON)
# =============================================================================

def canonical_json(obj: Any) -> str:
    """Convert object to canonical JSON representation."""
    def sort_keys(d):
        if isinstance(d, dict):
            return OrderedDict(sorted((k, sort_keys(v)) for k, v in d.items()))
        elif isinstance(d, list):
            return [sort_keys(item) for item in d]
        return d

    sorted_obj = sort_keys(obj)
    return json.dumps(sorted_obj, separators=(',', ':'), ensure_ascii=False) + '\n'


# =============================================================================
# Deep Merge
# =============================================================================

def deep_merge(target: Dict, source: Dict, path: str = "") -> Dict:
    """
    Deep merge source into target.
    - Objects: merge by key, right wins
    - Arrays: replace entirely
    - Scalars: replace
    Returns a new dict (doesn't modify target).
    Raises ConfigError on type conflicts.
    """
    result = target.copy() if isinstance(target, dict) else {}

    for key, value in source.items():
        current_path = f"{path}/{key}" if path else f"/{key}"

        if key in result:
            existing = result[key]
            # Type conflict detection
            if isinstance(existing, dict) and isinstance(value, dict):
                result[key] = deep_merge(existing, value, current_path)
            elif isinstance(existing, list) and isinstance(value, list):
                # Arrays: replace entirely
                result[key] = value.copy()
            elif isinstance(existing, (dict, list)) != isinstance(value, (dict, list)):
                # Type conflict: dict vs array
                raise ConfigError(
                    "unprocessable",
                    f"Type conflict at {current_path}: cannot merge {type(existing).__name__} with {type(value).__name__}",
                    details={"path": current_path}
                )
            else:
                # Scalar: replace
                result[key] = value
        else:
            # New key - deep copy if needed
            if isinstance(value, dict):
                result[key] = {**value}
            elif isinstance(value, list):
                result[key] = value.copy()
            else:
                result[key] = value

    return result


# =============================================================================
# Resolver with Cycle Detection
# =============================================================================

@dataclass
class ResolutionResult:
    """Result of a config resolution."""
    resolved_config: Dict
    resolution_graph: List[Dict]
    version_used: int


class ConfigResolver:
    """Resolves configurations with includes and cycle detection."""

    def __init__(self, storage: ConfigStorage):
        self.storage = storage

    def resolve(self, name: str, scope: Scope, version: Optional[int] = None,
                dry_run: bool = False) -> ResolutionResult:
        """
        Resolve a configuration with all includes applied.
        If version is None, uses the active version.
        If dry_run is True, doesn't require the config to exist.
        """
        # Get the starting config
        if not dry_run and version is None:
            entry = self.storage.get_active(name, scope)
            if entry is None:
                raise ConfigError("not_found", f"No active version exists for ({name}, {scope.as_dict()})", status=404)
            version = entry.version
        elif not dry_run and version is not None:
            entry = self.storage.get_version(name, scope, version)
            if entry is None:
                raise ConfigError("not_found", f"Version {version} not found for ({name}, {scope.as_dict()})", status=404)
        elif dry_run and version is not None:
            entry = self.storage.get_version(name, scope, version)
        else:
            entry = None

        visited: Set[Tuple[str, Scope, int]] = set()
        resolution_graph: List[Dict] = []

        def resolve_inner(n: str, s: Scope, v: Optional[int],
                         path: List[Tuple[str, Scope, int]]) -> Dict:
            """Recursively resolve a config with includes."""
            # Determine which version to use
            if v is None:
                store_entry = self.storage.get_active(n, s)
                if store_entry is None:
                    raise ConfigError(
                        "not_found",
                        f"Referenced config ({n}, {s.as_dict()}, active) does not exist",
                        details={"name": n, "scope": s.as_dict()}
                    )
                actual_version = store_entry.version
            else:
                store_entry = self.storage.get_version(n, s, v)
                if store_entry is None:
                    raise ConfigError(
                        "not_found",
                        f"Referenced config ({n}, {s.as_dict()}, version {v}) does not exist",
                        details={"name": n, "scope": s.as_dict(), "version": v}
                    )
                actual_version = v

            # Check cycle
            cycle_key = (n, s, actual_version)
            if cycle_key in visited:
                # Build cycle path for error message
                raise ConfigError(
                    "cycle_detected",
                    f"Cycle detected involving ({n}, {s.as_dict()}, version {actual_version})",
                    details={"cycle": [str(k) for k in path + [cycle_key]]}
                )

            if len(visited) >= self.storage.MAX_INCLUDE_DEPTH:
                raise ConfigError(
                    "unprocessable",
                    "Maximum include chain depth exceeded",
                    details={"reason": "max_depth"}
                )

            visited.add(cycle_key)

            # Add to resolution graph in merge order
            resolution_graph.append({
                "name": n,
                "scope": s.as_dict(),
                "version_used": actual_version
            })

            # Start with empty object
            merged = {}

            # Process includes in order
            for include in store_entry.includes:
                included = resolve_inner(include.name, include.scope, include.version, path + [(n, s, actual_version)])
                merged = deep_merge(merged, included)

            # Finally, merge own config on top
            merged = deep_merge(merged, store_entry.config)

            return merged

        if entry is None:
            # This is a dry_run without version - we can't resolve
            raise ConfigError("not_found", "Config does not exist", status=404)

        resolved = resolve_inner(name, scope, version, [])

        return ResolutionResult(
            resolved_config=resolved,
            resolution_graph=resolution_graph,
            version_used=version if version is not None else entry.version
        )


# =============================================================================
# Idempotency
# =============================================================================

class IdempotencyManager:
    """Tracks request bodies for idempotency."""

    def __init__(self):
        self._seen: Dict[str, str] = {}  # keyed by (name + body_hash)

    def check(self, name: str, body_json: str) -> bool:
        """Check if this exact body was seen for this name. Returns True if already seen."""
        key = f"{name}:{body_json}"
        return key in self._seen

    def record(self, name: str, body_json: str):
        """Record that this body was seen."""
        key = f"{name}:{body_json}"
        self._seen[key] = body_json


# =============================================================================
# Service
# =============================================================================

class ConfigService:
    """Main configuration service."""

    def __init__(self):
        self.storage = ConfigStorage()
        self.resolver = ConfigResolver(self.storage)
        self.idempotency = IdempotencyManager()

    def create_config(self, name: str, scope_dict: Dict, config: Dict,
                      includes: List[Dict], inherits_active: bool) -> Tuple:
        """Create a new config version."""
        # Check idempotency - normalize body for comparison
        body = {
            "scope": scope_dict,
            "config": config,
            "includes": includes,
            "inherits_active": inherits_active
        }
        body_json = canonical_json(body).strip()

        if self.idempotency.check(name, body_json):
            # Already exists - return existing active version info
            scope = Scope(scope_dict)
            store = self.storage.get_store(name, scope)
            if store and store.get_active():
                active = store.get_active()
                return (
                    json.dumps({
                        "name": name,
                        "scope": scope_dict,
                        "version": active.version,
                        "active": True
                    }, separators=(',', ':'), ensure_ascii=False) + '\n',
                    201,
                    {'Content-Type': 'application/json; charset=utf-8'}
                )

        # Validate and parse scope
        try:
            scope = Scope(scope_dict)
        except ConfigError:
            raise
        except Exception as e:
            raise ConfigError("invalid_input", f"Invalid scope: {e}")

        # Validate and parse includes
        parsed_includes = []
        for inc in includes:
            try:
                parsed_includes.append(IncludeRef.from_dict(inc))
            except ConfigError:
                raise
            except Exception as e:
                raise ConfigError("invalid_input", f"Invalid include reference: {e}")

        # Validate config
        if not isinstance(config, dict):
            raise ConfigError("invalid_input", "Config must be a JSON object")

        # Create version
        try:
            version, is_active = self.storage.create_version(
                name, scope, config, parsed_includes, inherits_active
            )
        except ConfigError:
            raise
        except Exception as e:
            raise ConfigError("internal", f"Failed to create version: {e}")

        self.idempotency.record(name, body_json)

        return (
            json.dumps({
                "name": name,
                "scope": scope_dict,
                "version": version,
                "active": is_active
            }, separators=(',', ':'), ensure_ascii=False) + '\n',
            201,
            {'Content-Type': 'application/json; charset=utf-8'}
        )

    def list_versions(self, name: str, scope_dict: Dict) -> Tuple:
        """List all versions for a (name, scope) pair."""
        try:
            scope = Scope(scope_dict)
        except Exception as e:
            return error_response("invalid_input", f"Invalid scope: {e}")

        versions = self.storage.list_versions(name, scope)

        return (
            json.dumps({
                "name": name,
                "scope": scope_dict,
                "versions": versions
            }, separators=(',', ':'), ensure_ascii=False) + '\n',
            200,
            {'Content-Type': 'application/json; charset=utf-8'}
        )

    def get_version(self, name: str, scope_dict: Dict, version: int) -> Tuple:
        """Get a specific version."""
        try:
            scope = Scope(scope_dict)
        except Exception as e:
            return error_response("invalid_input", f"Invalid scope: {e}")

        entry = self.storage.get_version(name, scope, version)
        if entry is None:
            return error_response("not_found", f"Version {version} not found for ({name}, {scope_dict})",
                                status=404)

        return (
            json.dumps({
                "name": name,
                "scope": scope_dict,
                "version": entry.version,
                "active": entry.active,
                "config": entry.config,
                "includes": [inc.to_dict() for inc in entry.includes]
            }, separators=(',', ':'), ensure_ascii=False) + '\n',
            200,
            {'Content-Type': 'application/json; charset=utf-8'}
        )

    def get_active(self, name: str, scope_dict: Dict) -> Tuple:
        """Get the active version."""
        try:
            scope = Scope(scope_dict)
        except Exception as e:
            return error_response("invalid_input", f"Invalid scope: {e}")

        entry = self.storage.get_active(name, scope)
        if entry is None:
            return error_response("not_found", f"No active version for ({name}, {scope_dict})", status=404)

        return (
            json.dumps({
                "name": name,
                "scope": scope_dict,
                "version": entry.version,
                "active": entry.active,
                "config": entry.config,
                "includes": [inc.to_dict() for inc in entry.includes]
            }, separators=(',', ':'), ensure_ascii=False) + '\n',
            200,
            {'Content-Type': 'application/json; charset=utf-8'}
        )

    def activate_version(self, name: str, scope_dict: Dict, version: int) -> Tuple:
        """Activate a specific version."""
        try:
            scope = Scope(scope_dict)
        except Exception as e:
            return error_response("invalid_input", f"Invalid scope: {e}")

        success = self.storage.activate_version(name, scope, version)
        if not success:
            return error_response("not_found", f"Version {version} not found for ({name}, {scope_dict})",
                                status=404)

        return (
            json.dumps({
                "name": name,
                "scope": scope_dict,
                "version": version,
                "active": True
            }, separators=(',', ':'), ensure_ascii=False) + '\n',
            200,
            {'Content-Type': 'application/json; charset=utf-8'}
        )

    def rollback(self, name: str, scope_dict: Dict, to_version: int) -> Tuple:
        """Rollback to a specific version."""
        try:
            scope = Scope(scope_dict)
        except Exception as e:
            return error_response("invalid_input", f"Invalid scope: {e}")

        # Check if version exists
        entry = self.storage.get_version(name, scope, to_version)
        if entry is None:
            return error_response("not_found", f"Version {to_version} not found for ({name}, {scope_dict})",
                                status=404)

        # Get current active
        active = self.storage.get_active(name, scope)
        if active is not None and to_version >= active.version:
            return error_response(
                "conflict",
                f"Cannot rollback to version {to_version} when active is {active.version} or newer",
                status=409
            )

        return self.activate_version(name, scope_dict, to_version)

    def resolve(self, name: str, scope_dict: Dict, version: Optional[int], dry_run: bool) -> Tuple:
        """Resolve a configuration with includes."""
        try:
            scope = Scope(scope_dict)
        except Exception as e:
            return error_response("invalid_input", f"Invalid scope: {e}")

        try:
            result = self.resolver.resolve(name, scope, version, dry_run)
        except ConfigError as e:
            return e.args[0], e.status, {'Content-Type': 'application/json; charset=utf-8'}
        except Exception as e:
            return error_response("internal", f"Resolution failed: {e}")

        return (
            json.dumps({
                "name": name,
                "scope": scope_dict,
                "version_used": result.version_used,
                "resolved_config": result.resolved_config,
                "resolution_graph": result.resolution_graph
            }, separators=(',', ':'), ensure_ascii=False) + '\n',
            200,
            {'Content-Type': 'application/json; charset=utf-8'}
        )


# =============================================================================
# Flask Application
# =============================================================================

service = ConfigService()


def parse_json_body(max_size: int = 1024 * 1024) -> Dict:
    """Parse JSON body with size limit."""
    if not request.data:
        raise ConfigError("invalid_input", "Request body is required")

    if len(request.data) > max_size:
        raise ConfigError("too_large", f"Request body exceeds {max_size} bytes", status=413)

    try:
        return request.get_json(force=True, silent=False)
    except Exception as e:
        raise ConfigError("invalid_input", f"Invalid JSON: {e}")


@app.route('/healthz', methods=['GET'])
def healthz():
    """Health check endpoint."""
    return (
        json.dumps({"ok": True}, separators=(',', ':')) + '\n',
        200,
        {'Content-Type': 'application/json; charset=utf-8'}
    )


@app.route('/v1/configs/<name>', methods=['POST'])
def create_config(name):
    """Create a new config version."""
    try:
        body = parse_json_body()

        # Validate required fields
        if 'scope' not in body:
            raise ConfigError("invalid_input", "Missing required field: scope")
        if 'config' not in body:
            raise ConfigError("invalid_input", "Missing required field: config")

        scope = body['scope']
        config = body['config']
        includes = body.get('includes', [])
        inherits_active = body.get('inherits_active', False)

        if not isinstance(scope, dict):
            raise ConfigError("invalid_input", "Scope must be an object")
        if not isinstance(config, dict):
            raise ConfigError("invalid_input", "Config must be an object")
        if not isinstance(includes, list):
            raise ConfigError("invalid_input", "Includes must be a list")
        if not isinstance(inherits_active, bool):
            raise ConfigError("invalid_input", "inherits_active must be a boolean")

        return service.create_config(name, scope, config, includes, inherits_active)

    except ConfigError as e:
        return error_response(e.code, e.message, e.details, e.status)
    except Exception as e:
        return error_response("internal", str(e))


@app.route('/v1/configs/<name>:versions', methods=['POST'])
def list_versions(name):
    """List all versions for a (name, scope) pair."""
    try:
        body = parse_json_body()
        if 'scope' not in body:
            raise ConfigError("invalid_input", "Missing required field: scope")
        return service.list_versions(name, body['scope'])
    except ConfigError as e:
        return error_response(e.code, e.message, e.details, e.status)
    except Exception as e:
        return error_response("internal", str(e))


@app.route('/v1/configs/<name>/<int:version>', methods=['POST'])
def get_version(name, version):
    """Get a specific version."""
    try:
        body = parse_json_body()
        if 'scope' not in body:
            raise ConfigError("invalid_input", "Missing required field: scope")
        return service.get_version(name, body['scope'], version)
    except ConfigError as e:
        return error_response(e.code, e.message, e.details, e.status)
    except Exception as e:
        return error_response("internal", str(e))


@app.route('/v1/configs/<name>:active', methods=['POST'])
def get_active(name):
    """Get the active version."""
    try:
        body = parse_json_body()
        if 'scope' not in body:
            raise ConfigError("invalid_input", "Missing required field: scope")
        return service.get_active(name, body['scope'])
    except ConfigError as e:
        return error_response(e.code, e.message, e.details, e.status)
    except Exception as e:
        return error_response("internal", str(e))


@app.route('/v1/configs/<name>/<int:version>:activate', methods=['POST'])
def activate_version(name, version):
    """Activate a specific version."""
    try:
        body = parse_json_body()
        if 'scope' not in body:
            raise ConfigError("invalid_input", "Missing required field: scope")
        return service.activate_version(name, body['scope'], version)
    except ConfigError as e:
        return error_response(e.code, e.message, e.details, e.status)
    except Exception as e:
        return error_response("internal", str(e))


@app.route('/v1/configs/<name>:rollback', methods=['POST'])
def rollback(name):
    """Rollback to a specific version."""
    try:
        body = parse_json_body()
        if 'scope' not in body:
            raise ConfigError("invalid_input", "Missing required field: scope")
        if 'to_version' not in body:
            raise ConfigError("invalid_input", "Missing required field: to_version")

        to_version = body['to_version']
        if not isinstance(to_version, int) or to_version < 1:
            raise ConfigError("invalid_input", "to_version must be a positive integer")

        return service.rollback(name, body['scope'], to_version)
    except ConfigError as e:
        return error_response(e.code, e.message, e.details, e.status)
    except Exception as e:
        return error_response("internal", str(e))


@app.route('/v1/configs/<name>:resolve', methods=['POST'])
def resolve(name):
    """Resolve a configuration with includes."""
    try:
        body = parse_json_body()
        if 'scope' not in body:
            raise ConfigError("invalid_input", "Missing required field: scope")

        version = body.get('version')
        if version is not None and version is not None:
            if version is None:
                version = None
            elif not isinstance(version, int) or version < 1:
                raise ConfigError("invalid_input", "version must be a positive integer or null")

        dry_run = body.get('dry_run', False)
        if not isinstance(dry_run, bool):
            raise ConfigError("invalid_input", "dry_run must be a boolean")

        return service.resolve(name, body['scope'], version, dry_run)
    except ConfigError as e:
        return error_response(e.code, e.message, e.details, e.status)
    except Exception as e:
        return error_response("internal", str(e))


# =============================================================================
# Error Handlers
# =============================================================================

@app.errorhandler(404)
def not_found(e):
    return error_response("not_found", "Resource not found", status=404)


@app.errorhandler(405)
def method_not_allowed(e):
    return error_response("invalid_input", "Method not allowed", status=405)


@app.errorhandler(413)
def request_too_large(e):
    return error_response("too_large", "Request body too large", status=413)


@app.errorhandler(Exception)
def internal_error(e):
    return error_response("internal", str(e), status=500)


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description='Configuration Management Service')
    parser.add_argument('--address', default='0.0.0.0', help='Address to bind to (default: 0.0.0.0)')
    parser.add_argument('--port', type=int, default=8080, help='Port to listen on (default: 8080)')
    args = parser.parse_args()

    app.run(host=args.address, port=args.port, debug=False)


if __name__ == '__main__':
    main()
