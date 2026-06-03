#!/usr/bin/env python3
"""
Configuration Management Service - Immutable versioned configs with scoping,
rollback, and import/inheritance support.
"""

import json
import sys
from dataclasses import dataclass, field
from typing import Optional, Any, Dict, List, Tuple, Set
from flask import Flask, request, jsonify, Response
import argparse

app = Flask(__name__)

# =============================================================================
# Data Models
# =============================================================================

@dataclass(frozen=True)
class Scope:
    """Immutable scope represented as a frozen dict for hashing."""
    values: Tuple[Tuple[str, str], ...] = field(default_factory=tuple)

    def __init__(self, d: Optional[Dict[str, str]] = None):
        if d is None:
            d = {}
        if not isinstance(d, dict):
            raise ValueError("Scope must be a dict")
        for k, v in d.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise ValueError("Scope keys and values must be strings")
        object.__setattr__(self, 'values', tuple(sorted(d.items())))

    def as_dict(self) -> Dict[str, str]:
        return dict(self.values)

    def to_dict(self) -> Dict[str, str]:
        return dict(self.values)

    def __hash__(self):
        return hash(self.values)

    def __eq__(self, other):
        if not isinstance(other, Scope):
            return False
        return self.values == other.values


@dataclass(frozen=True)
class IncludeRef:
    """Reference to another config."""
    name: str
    scope: Scope
    version: Optional[int]  # None means use active at resolution time

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("Include name must be non-empty string")
        if not isinstance(self.scope, Scope):
            raise ValueError("Include scope must be a Scope")
        if self.version is not None and (not isinstance(self.version, int) or self.version < 1):
            raise ValueError("Include version must be a positive integer or null")

    def to_dict(self) -> Dict[str, Any]:
        result = {"name": self.name, "scope": self.scope.to_dict()}
        if self.version is not None:
            result["version"] = self.version
        return result

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'IncludeRef':
        if not isinstance(d, dict):
            raise ValueError("Include reference must be an object")
        if "name" not in d:
            raise ValueError("Include reference must have 'name'")
        if "scope" not in d:
            raise ValueError("Include reference must have 'scope'")
        name = d["name"]
        scope = Scope(d["scope"])
        version = d.get("version")
        if version is not None and not isinstance(version, int):
            raise ValueError("Include version must be integer or null")
        return cls(name=name, scope=scope, version=version)


@dataclass(frozen=True)
class Config:
    """An immutable version of a config."""
    config: Dict[str, Any]
    includes: Tuple[IncludeRef, ...]
    version: int

    def __post_init__(self):
        if not isinstance(self.config, dict):
            raise ValueError("Config must be a dict")
        if not isinstance(self.includes, tuple):
            object.__setattr__(self, 'includes', tuple(self.includes))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "config": self.config,
            "includes": [inc.to_dict() for inc in self.includes]
        }


@dataclass(frozen=True)
class ConfigKey:
    """Key for a config (name, scope)."""
    name: str
    scope: Scope

    def __hash__(self):
        return hash((self.name, self.scope))

    def __eq__(self, other):
        if not isinstance(other, ConfigKey):
            return False
        return self.name == other.name and self.scope == other.scope


# =============================================================================
# Storage
# =============================================================================

class ConfigStorage:
    """In-memory storage for configs."""

    def __init__(self):
        # key -> { version -> Config }
        self._configs: Dict[ConfigKey, Dict[int, Config]] = {}
        # key -> active version
        self._active: Dict[ConfigKey, int] = {}
        # key -> next version number
        self._next_version: Dict[ConfigKey, int] = {}

    def create_version(self, name: str, scope: Scope, config: Dict[str, Any],
                       includes: List[IncludeRef], allow_overflow: bool = True) -> Tuple[int, bool]:
        """
        Create a new immutable version.
        Returns (version, was_active).
        """
        key = ConfigKey(name, scope)

        if key not in self._next_version:
            self._next_version[key] = 1
            self._configs[key] = {}

        current_next = self._next_version[key]
        if len(self._configs[key]) >= 10000:
            raise ValueError("Max versions exceeded")

        version = current_next
        config_obj = Config(config=config, includes=tuple(includes), version=version)

        self._configs[key][version] = config_obj
        self._next_version[key] = version + 1

        # Check if this should be active
        was_active = key not in self._active or version > self._active[key]
        if was_active:
            self._active[key] = version

        return version, was_active

    def get_versions(self, name: str, scope: Scope) -> List[Dict[str, Any]]:
        """Get all versions for a key."""
        key = ConfigKey(name, scope)
        if key not in self._configs:
            return []

        active_version = self._active.get(key)
        result = []
        for v in sorted(self._configs[key].keys()):
            result.append({
                "version": v,
                "active": v == active_version
            })
        return result

    def get_version(self, name: str, scope: Scope, version: int) -> Optional[Config]:
        """Get a specific version."""
        key = ConfigKey(name, scope)
        if key not in self._configs:
            return None
        return self._configs[key].get(version)

    def get_active(self, name: str, scope: Scope) -> Optional[Config]:
        """Get the active version."""
        key = ConfigKey(name, scope)
        if key not in self._active:
            return None
        return self._configs[key].get(self._active[key])

    def activate_version(self, name: str, scope: Scope, version: int) -> bool:
        """
        Activate a specific version.
        Returns True if version existed and was activated.
        """
        key = ConfigKey(name, scope)
        if key not in self._configs or version not in self._configs[key]:
            return False
        self._active[key] = version
        return True

    def version_exists(self, name: str, scope: Scope, version: int) -> bool:
        """Check if a version exists."""
        key = ConfigKey(name, scope)
        return key in self._configs and version in self._configs[key]

    def get_active_version(self, name: str, scope: Scope) -> Optional[int]:
        """Get the active version number."""
        key = ConfigKey(name, scope)
        return self._active.get(key)


# =============================================================================
# Deep Merge
# =============================================================================

def deep_merge(base: Dict[str, Any], override: Dict[str, Any], path: str = "") -> Dict[str, Any]:
    """
    Deep merge two dictionaries.
    Raises ValueError on type conflicts with path info.
    """
    result = dict(base)

    for key, value in override.items():
        current_path = f"{path}/{key}" if path else f"/{key}"

        if key in result:
            base_val = result[key]
            # Type conflict detection
            if isinstance(base_val, dict) and isinstance(value, dict):
                result[key] = deep_merge(base_val, value, current_path)
            elif isinstance(base_val, list) or isinstance(value, list):
                # Arrays replace entirely
                result[key] = value
            else:
                # Scalars replace
                if type(base_val) != type(value):
                    raise ValueError(f"Type conflict at {current_path}: {type(base_val).__name__} vs {type(value).__name__}")
                result[key] = value
        else:
            result[key] = value

    return result


# =============================================================================
# Include Resolution
# =============================================================================

@dataclass
class ResolutionContext:
    """Context for tracking resolution state."""
    visited: Set[Tuple[str, Tuple[Tuple[str, str], ...], Optional[int]]] = field(default_factory=set)
    resolution_graph: List[Dict[str, Any]] = field(default_factory=list)
    max_depth: int = 64


def resolve_config(storage: ConfigStorage, name: str, scope: Scope,
                   version: Optional[int], context: ResolutionContext,
                   dry_run: bool = False) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    Resolve a config with all includes applied.
    Returns (resolved_config, resolution_graph_segment).
    """
    # Determine version to use
    if version is None:
        if dry_run:
            raise ValueError("version required for dry_run")
        version = storage.get_active_version(name, scope)
        if version is None:
            raise ValueError("no active version")

    # Check cycle
    key_tuple = (name, scope.values, version)
    if key_tuple in context.visited:
        raise ValueError("cycle_detected")

    if len(context.visited) > context.max_depth:
        raise ValueError("max_depth")

    # Add to visited
    context.visited.add(key_tuple)

    try:
        # Get config
        config = storage.get_version(name, scope, version)
        if config is None:
            if dry_run:
                return {}, []
            raise ValueError("not_found")

        # Start with empty object
        result: Dict[str, Any] = {}

        # Process includes in order
        for include_ref in config.includes:
            include_result, include_graph = resolve_config(
                storage, include_ref.name, include_ref.scope,
                include_ref.version, context, dry_run
            )
            # Merge include result
            try:
                result = deep_merge(result, include_result)
            except ValueError as e:
                if "Type conflict" in str(e):
                    raise ValueError(f"unprocessable: {str(e)}")
                raise
            # Add to resolution graph if not already there
            for node in include_graph:
                key = (node["name"], tuple(sorted(node["scope"].items())),
                       node["version_used"])
                if node not in context.resolution_graph:
                    context.resolution_graph.append(node)

        # Add current config to graph
        context.resolution_graph.append({
            "name": name,
            "scope": scope.to_dict(),
            "version_used": version
        })

        # Merge own config on top
        try:
            result = deep_merge(result, config.config)
        except ValueError as e:
            if "Type conflict" in str(e):
                raise ValueError(f"unprocessable: {str(e)}")
            raise

        return result, context.resolution_graph.copy()

    finally:
        context.visited.discard(key_tuple)


# =============================================================================
# JSON Normalization (Canonical JSON)
# =============================================================================

def canonical_json(obj: Any) -> str:
    """
    Produce canonical JSON with sorted keys, minimal representation.
    """
    def sort_keys(d):
        if isinstance(d, dict):
            return {k: sort_keys(v) for k, v in sorted(d.items())}
        if isinstance(d, list):
            return [sort_keys(item) for item in d]
        return d

    sorted_obj = sort_keys(obj)
    # Use separators without spaces
    return json.dumps(sorted_obj, separators=(',', ':'), ensure_ascii=False) + "\n"


# =============================================================================
# Error Responses
# =============================================================================

def error_response(code: str, message: str, details: Optional[Dict] = None) -> Tuple[Response, int]:
    """Create a JSON error response."""
    if details is None:
        details = {}
    body = {
        "error": {
            "code": code,
            "message": message,
            "details": details
        }
    }
    return Response(
        canonical_json(body),
        mimetype="application/json; charset=utf-8"
    ), _error_status(code)


def _error_status(code: str) -> int:
    status_map = {
        "invalid_input": 400,
        "not_found": 404,
        "conflict": 409,
        "cycle_detected": 409,
        "unprocessable": 422,
        "rate_limited": 429,
        "internal": 500,
        "too_large": 413
    }
    return status_map.get(code, 500)


# =============================================================================
# Request Helpers
# =============================================================================

def parse_scope(data: Dict[str, Any], field: str = "scope") -> Scope:
    """Parse scope from request data."""
    if field not in data:
        raise ValueError(f"Missing '{field}'")
    scope_data = data[field]
    if not isinstance(scope_data, dict):
        raise ValueError(f"'{field}' must be an object")
    return Scope(scope_data)


def validate_include_ref(data: Dict[str, Any]) -> IncludeRef:
    """Validate and parse an include reference."""
    return IncludeRef.from_dict(data)


def get_json_request(max_size: int = 1024 * 1024) -> Dict[str, Any]:
    """Get and validate JSON from request."""
    if request.content_length and request.content_length > max_size:
        return error_response("too_large", "Request body too large")

    if not request.is_json:
        return error_response("invalid_input", "Content-Type must be application/json")

    try:
        data = request.get_json()
        if data is None:
            return error_response("invalid_input", "Invalid JSON")
        return data
    except Exception:
        return error_response("invalid_input", "Failed to parse JSON")


# =============================================================================
# Global Storage
# =============================================================================

storage = ConfigStorage()

# Idempotency tracking for create
# (name, scope_dict_str, config_str) -> version
create_cache: Dict[str, int] = {}


def get_idempotency_key(name: str, scope: Scope, config: Dict[str, Any]) -> str:
    """Generate idempotency key for create."""
    # Normalize config for comparison
    config_str = canonical_json(config)
    scope_str = canonical_json(scope.to_dict())
    return f"{name}:{scope_str}:{config_str}"


# =============================================================================
# Flask Routes
# =============================================================================

@app.route('/healthz', methods=['GET'])
def healthz():
    """Health check endpoint."""
    return Response(
        canonical_json({"ok": True}),
        mimetype="application/json; charset=utf-8"
    ), 200


@app.route('/v1/configs/<name>', methods=['POST'])
def create_config(name: str):
    """Create a new version of a config."""
    global storage, create_cache

    # Parse request
    data = get_json_request()
    if isinstance(data, tuple):
        return data  # Error response

    try:
        scope = parse_scope(data)
    except ValueError as e:
        return error_response("invalid_input", str(e))

    if "config" not in data:
        return error_response("invalid_input", "Missing 'config'")

    config_data = data["config"]
    if not isinstance(config_data, dict):
        return error_response("invalid_input", "'config' must be an object")

    includes = []
    if "includes" in data:
        if data["includes"] is not None:
            if not isinstance(data["includes"], list):
                return error_response("invalid_input", "'includes' must be a list")
            for inc_data in data["includes"]:
                try:
                    includes.append(validate_include_ref(inc_data))
                except ValueError as e:
                    return error_response("invalid_input", str(e))

    inherits_active = data.get("inherits_active", False)
    if not isinstance(inherits_active, bool):
        return error_response("invalid_input", "'inherits_active' must be boolean")

    # Check idempotency
    idempotency_key = get_idempotency_key(name, scope, config_data)
    if idempotency_key in create_cache:
        # Return existing version
        existing_version = create_cache[idempotency_key]
        key = ConfigKey(name, scope)
        active_version = storage.get_active_version(name, scope)
        return Response(
            canonical_json({
                "name": name,
                "scope": scope.to_dict(),
                "version": existing_version,
                "active": existing_version == active_version
            }),
            mimetype="application/json; charset=utf-8"
        ), 201

    # Handle inherits_active
    if inherits_active:
        active_config = storage.get_active(name, scope)
        if active_config is not None:
            # Merge active config into new config
            # Active config's values take precedence (child overrides parent)
            merged_config = deep_merge(active_config.config, config_data)
            merged_includes = list(active_config.includes) + includes
            config_data = merged_config
            includes = merged_includes

    try:
        version, is_active = storage.create_version(name, scope, config_data, includes)
    except ValueError as e:
        if "Max versions exceeded" in str(e):
            return error_response("conflict", str(e))
        return error_response("internal", str(e))

    # Cache for idempotency
    create_cache[idempotency_key] = version

    response = {
        "name": name,
        "scope": scope.to_dict(),
        "version": version,
        "active": is_active
    }
    return Response(
        canonical_json(response),
        mimetype="application/json; charset=utf-8"
    ), 201


@app.route('/v1/configs/<name>:versions', methods=['POST'])
def list_versions(name: str):
    """List all versions for a (name, scope)."""
    data = get_json_request()
    if isinstance(data, tuple):
        return data

    try:
        scope = parse_scope(data)
    except ValueError as e:
        return error_response("invalid_input", str(e))

    versions = storage.get_versions(name, scope)

    response = {
        "name": name,
        "scope": scope.to_dict(),
        "versions": versions
    }
    return Response(
        canonical_json(response),
        mimetype="application/json; charset=utf-8"
    ), 200


@app.route('/v1/configs/<name>/<int:version>', methods=['POST'])
def get_version(name: str, version: int):
    """Get a specific raw version."""
    data = get_json_request()
    if isinstance(data, tuple):
        return data

    try:
        scope = parse_scope(data)
    except ValueError as e:
        return error_response("invalid_input", str(e))

    config = storage.get_version(name, scope, version)
    if config is None:
        return error_response("not_found", f"Config {name} with version {version} not found for scope")

    active_version = storage.get_active_version(name, scope)

    response = {
        "name": name,
        "scope": scope.to_dict(),
        "version": config.version,
        "active": config.version == active_version,
        "config": config.config,
        "includes": [inc.to_dict() for inc in config.includes]
    }
    return Response(
        canonical_json(response),
        mimetype="application/json; charset=utf-8"
    ), 200


@app.route('/v1/configs/<name>:active', methods=['POST'])
def get_active(name: str):
    """Get the active raw version."""
    data = get_json_request()
    if isinstance(data, tuple):
        return data

    try:
        scope = parse_scope(data)
    except ValueError as e:
        return error_response("invalid_input", str(e))

    config = storage.get_active(name, scope)
    if config is None:
        return error_response("not_found", f"No active config found for {name} with scope")

    response = {
        "name": name,
        "scope": scope.to_dict(),
        "version": config.version,
        "active": True,
        "config": config.config,
        "includes": [inc.to_dict() for inc in config.includes]
    }
    return Response(
        canonical_json(response),
        mimetype="application/json; charset=utf-8"
    ), 200


@app.route('/v1/configs/<name>/<int:version>:activate', methods=['POST'])
def activate_version(name: str, version: int):
    """Activate a specific version."""
    data = get_json_request()
    if isinstance(data, tuple):
        return data

    try:
        scope = parse_scope(data)
    except ValueError as e:
        return error_response("invalid_input", str(e))

    if not storage.version_exists(name, scope, version):
        return error_response("not_found", f"Config {name} version {version} not found for scope")

    storage.activate_version(name, scope, version)

    response = {
        "name": name,
        "scope": scope.to_dict(),
        "version": version,
        "active": True
    }
    return Response(
        canonical_json(response),
        mimetype="application/json; charset=utf-8"
    ), 200


@app.route('/v1/configs/<name>:rollback', methods=['POST'])
def rollback(name: str):
    """Rollback to an earlier version."""
    data = get_json_request()
    if isinstance(data, tuple):
        return data

    if "to_version" not in data:
        return error_response("invalid_input", "Missing 'to_version'")

    try:
        to_version = int(data["to_version"])
    except (ValueError, TypeError):
        return error_response("invalid_input", "'to_version' must be an integer")

    try:
        scope = parse_scope(data)
    except ValueError as e:
        return error_response("invalid_input", str(e))

    # Check if target version exists
    if not storage.version_exists(name, scope, to_version):
        return error_response("not_found", f"Config {name} version {to_version} not found for scope")

    current_active = storage.get_active_version(name, scope)

    # Can only rollback to earlier or equal version
    if current_active is not None and to_version > current_active:
        return error_response("conflict", f"Cannot rollback to version {to_version}, current active is {current_active}")

    storage.activate_version(name, scope, to_version)

    response = {
        "name": name,
        "scope": scope.to_dict(),
        "version": to_version,
        "active": True
    }
    return Response(
        canonical_json(response),
        mimetype="application/json; charset=utf-8"
    ), 200


@app.route('/v1/configs/<name>:resolve', methods=['POST'])
def resolve(name: str):
    """Resolve a config with all includes applied."""
    data = get_json_request()
    if isinstance(data, tuple):
        return data

    try:
        scope = parse_scope(data)
    except ValueError as e:
        return error_response("invalid_input", str(e))

    version = data.get("version")
    if version is not None and not isinstance(version, int):
        return error_response("invalid_input", "'version' must be an integer")

    dry_run = data.get("dry_run", False)
    if not isinstance(dry_run, bool):
        return error_response("invalid_input", "'dry_run' must be boolean")

    context = ResolutionContext()

    try:
        resolved_config, resolution_graph = resolve_config(
            storage, name, scope, version, context, dry_run
        )
    except ValueError as e:
        error_msg = str(e)
        if "cycle_detected" in error_msg:
            return error_response("cycle_detected", "Cycle detected in include references")
        elif "max_depth" in error_msg:
            return error_response("unprocessable", "Max include chain depth exceeded",
                                 {"reason": "max_depth"})
        elif "not_found" in error_msg:
            # Find which config is missing
            return error_response("not_found", f"Referenced config not found for {name} with scope")
        elif "unprocessable" in error_msg:
            # Extract path from error message
            path = error_msg.split(":")[-1].strip() if ":" in error_msg else ""
            return error_response("unprocessable", "Merge type conflict",
                                 {"path": path})
        elif "version required" in error_msg:
            return error_response("invalid_input", error_msg)
        else:
            return error_response("internal", error_msg)

    # Determine version used
    if version is None:
        version_used = storage.get_active_version(name, scope)
    else:
        version_used = version

    response = {
        "name": name,
        "scope": scope.to_dict(),
        "version_used": version_used,
        "resolved_config": resolved_config,
        "resolution_graph": resolution_graph
    }
    return Response(
        canonical_json(response),
        mimetype="application/json; charset=utf-8"
    ), 200


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description='Config Management Service')
    parser.add_argument('--address', default='0.0.0.0', help='Address to bind to')
    parser.add_argument('--port', type=int, default=8080, help='Port to listen on')
    args = parser.parse_args()

    app.run(host=args.address, port=args.port, threaded=True)


if __name__ == '__main__':
    main()