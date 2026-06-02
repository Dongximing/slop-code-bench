#!/usr/bin/env python3
"""
Config Service - REST API for managing JSON configuration objects with
immutable versions, scoping, rollback, and import/inheritance.
"""

import asyncio
import hashlib
import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
import uvicorn


# =============================================================================
# Constants
# =============================================================================

MAX_REQUEST_SIZE = 1024 * 1024  # 1 MiB
MAX_INCLUDE_CHAIN = 64
MAX_VERSIONS_PER_SCOPE = 10_000

# Canonical JSON formatting constants
CANONICAL_SEPARATORS = (',', ':')
CANONICAL_INDENT = None  # Compact, but we'll add single \n at end


# =============================================================================
# Error handling
# =============================================================================

class ConfigError(Exception):
    """Base exception for config service errors."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(message)


class ValidationError(ConfigError):
    """Validation error."""

    pass


class NotFoundError(ConfigError):
    """Resource not found."""

    pass


class ConflictError(ConfigError):
    """Conflict error."""

    pass


class UnprocessableError(ConfigError):
    """Unprocessable entity."""

    pass


class CycleDetectedError(ConfigError):
    """Cycle detected in includes."""

    pass


class RateLimitError(ConfigError):
    """Rate limited."""

    pass


# =============================================================================
# Data models
# =============================================================================

@dataclass(frozen=True)
class Scope:
    """Represents a scope as a flat string-to-string mapping."""

    _data: dict[str, str]

    def __post_init__(self):
        object.__setattr__(self, '_data', dict(self._data))

    def keys(self):
        return self._data.keys()

    def values(self):
        return self._data.values()

    def items(self):
        return self._data.items()

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def __getitem__(self, key: str):
        return self._data[key]

    def __eq__(self, other):
        if not isinstance(other, Scope):
            return False
        return self._data == other._data

    def __hash__(self):
        # Hash based on sorted items
        return hash(tuple(sorted(self._data.items())))

    def to_dict(self) -> dict[str, str]:
        return dict(self._data)

    @classmethod
    def from_dict(cls, d: dict[str, str]) -> 'Scope':
        if not isinstance(d, dict):
            raise ValidationError('invalid_input', 'Scope must be a JSON object')
        for k, v in d.items():
            if not isinstance(k, str):
                raise ValidationError('invalid_input', f'Scope key must be string, got {type(k).__name__}')
            if not isinstance(v, str):
                raise ValidationError('invalid_input', f'Scope value must be string, got {type(v).__name__}')
        return cls(d)


@dataclass(frozen=True)
class IncludeRef:
    """Reference to another config."""

    name: str
    scope: Scope
    version: int | None  # None means use current active

    def to_dict(self) -> dict[str, Any]:
        result = {
            'name': self.name,
            'scope': self.scope.to_dict(),
        }
        if self.version is not None:
            result['version'] = self.version
        return result

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> 'IncludeRef':
        if not isinstance(d, dict):
            raise ValidationError('invalid_input', 'Include reference must be a JSON object')

        if 'name' not in d:
            raise ValidationError('invalid_input', 'Include reference must have "name"')
        if 'scope' not in d:
            raise ValidationError('invalid_input', 'Include reference must have "scope"')

        name = d['name']
        if not isinstance(name, str) or not name:
            raise ValidationError('invalid_input', 'Include name must be non-empty string')

        scope = Scope.from_dict(d['scope'])
        version = d.get('version')
        if version is not None and version is not None:  # noqa: E711
            if not isinstance(version, int) or version < 1:
                raise ValidationError('invalid_input', 'Version must be positive integer')

        return cls(name=name, scope=scope, version=version)


@dataclass(frozen=True)
class ConfigVersion:
    """An immutable version of a config."""

    name: str
    scope: Scope
    version: int
    config: dict[str, Any]
    includes: tuple[IncludeRef, ...] = ()
    active: bool = False

    def to_dict(self, include_config: bool = True) -> dict[str, Any]:
        result = {
            'name': self.name,
            'scope': self.scope.to_dict(),
            'version': self.version,
            'active': self.active,
        }
        if include_config:
            result['config'] = self.config
            result['includes'] = [inc.to_dict() for inc in self.includes]
        return result


# =============================================================================
# Storage
# =============================================================================

@dataclass
class ConfigStorage:
    """In-memory storage for configs. In production, would use a database."""

    # (name, scope_hash) -> list of ConfigVersion sorted by version
    _configs: dict[tuple[str, int], list[ConfigVersion]] = field(default_factory=dict)

    def _scope_key(self, name: str, scope: Scope) -> tuple[str, int]:
        """Generate a key for the (name, scope) pair."""
        # Use hash of scope for dictionary key
        scope_hash = hash(scope)
        return (name, scope_hash)

    def _get_versions(self, name: str, scope: Scope) -> list[ConfigVersion]:
        """Get all versions for a (name, scope) pair."""
        key = self._scope_key(name, scope)
        return self._configs.get(key, [])

    def create_version(
        self,
        name: str,
        scope: Scope,
        config: dict[str, Any],
        includes: list[IncludeRef],
        inherits_active: bool = False
    ) -> ConfigVersion:
        """Create a new immutable version."""
        key = self._scope_key(name, scope)
        versions = self._configs.get(key, [])

        # Check max versions
        if len(versions) >= MAX_VERSIONS_PER_SCOPE:
            raise ConflictError('conflict', f'Maximum {MAX_VERSIONS_PER_SCOPE} versions reached for {name}')

        # Determine next version number
        next_version = len(versions) + 1

        # Check idempotency - if identical request, return existing
        # Two configs are identical if same name, scope, config, and includes
        for v in versions:
            if v.config == config and v.includes == tuple(includes):
                # Return existing version, but ensure it's active
                if not v.active:
                    # Need to activate it
                    object.__setattr__(v, 'active', True)
                return v

        # If inherits_active is True, inherit omitted fields from active version
        if inherits_active and versions:
            active = versions[-1]  # Last one is active
            # Deep merge active.config into config (active wins for conflicts)
            config = deep_merge(active.config, config)

        # Deactivate all existing versions
        for v in versions:
            object.__setattr__(v, 'active', False)

        # Create new version
        new_version = ConfigVersion(
            name=name,
            scope=scope,
            version=next_version,
            config=config,
            includes=tuple(includes),
            active=True
        )

        # Store it
        self._configs[key] = versions + [new_version]

        return new_version

    def get_version(self, name: str, scope: Scope, version: int) -> ConfigVersion:
        """Get a specific version."""
        versions = self._get_versions(name, scope)
        for v in versions:
            if v.version == version:
                return v
        raise NotFoundError('not_found', f'Version {version} not found for {name}')

    def get_active(self, name: str, scope: Scope) -> ConfigVersion:
        """Get the active version."""
        versions = self._get_versions(name, scope)
        for v in reversed(versions):
            if v.active:
                return v
        raise NotFoundError('not_found', f'No active version for {name}')

    def list_versions(self, name: str, scope: Scope) -> list[ConfigVersion]:
        """List all versions for a (name, scope) pair."""
        return list(self._get_versions(name, scope))

    def activate_version(self, name: str, scope: Scope, version: int) -> ConfigVersion:
        """Activate a specific version."""
        versions = self._get_versions(name, scope)

        target = None
        for v in versions:
            if v.version == version:
                target = v
                break

        if target is None:
            raise NotFoundError('not_found', f'Version {version} not found for {name}')

        # Deactivate all others
        for v in versions:
            object.__setattr__(v, 'active', v.version == version)

        return target

    def rollback(self, name: str, scope: Scope, to_version: int) -> ConfigVersion:
        """Rollback to an earlier version."""
        versions = self._get_versions(name, scope)

        # Check if to_version exists
        target = None
        for v in versions:
            if v.version == to_version:
                target = v
                break

        if target is None:
            raise NotFoundError('not_found', f'Version {to_version} not found for {name}')

        # Find current active
        current_active = None
        for v in reversed(versions):
            if v.active:
                current_active = v
                break

        if current_active is None:
            raise NotFoundError('not_found', f'No active version for {name}')

        # Only allow rollback to earlier or same version
        if to_version > current_active.version:
            raise ConflictError('conflict', f'Cannot rollback to version {to_version} (newer than active {current_active.version})')

        return self.activate_version(name, scope, to_version)


# =============================================================================
# Deep merge with type checking
# =============================================================================

def deep_merge(base: dict[str, Any], override: dict[str, Any], path: str = '') -> dict[str, Any]:
    """
    Deep merge two dictionaries.
    Override values take precedence.
    Raises UnprocessableError on type conflicts.
    """
    result = dict(base)

    for key, value in override.items():
        current_path = f'{path}/{key}' if path else f'/{key}'

        if key in result:
            existing = result[key]

            # Type conflict detection
            if isinstance(existing, dict) and isinstance(value, dict):
                # Both are dicts, recurse
                result[key] = deep_merge(existing, value, current_path)
            elif isinstance(existing, list) and isinstance(value, list):
                # Both are lists, replace entirely (right wins)
                result[key] = value
            elif isinstance(existing, (str, int, float, bool, type(None))) and \
                 isinstance(value, (str, int, float, bool, type(None))):
                # Both are scalars, replace (right wins)
                result[key] = value
            else:
                # Type conflict
                raise UnprocessableError(
                    'unprocessable',
                    f'Type conflict at {current_path}: {type(existing).__name__} vs {type(value).__name__}',
                    {'path': current_path}
                )
        else:
            result[key] = value

    return result


# =============================================================================
# Resolution engine
# =============================================================================

@dataclass
class ResolutionNode:
    """A node in the resolution graph."""

    name: str
    scope: Scope
    version_used: int

    def to_dict(self) -> dict[str, Any]:
        return {
            'name': self.name,
            'scope': self.scope.to_dict(),
            'version_used': self.version_used,
        }


def resolve_config(
    storage: ConfigStorage,
    name: str,
    scope: Scope,
    version: int | None,
    visited: set[tuple[str, int, int]] | None = None
) -> tuple[dict[str, Any], list[ResolutionNode]]:
    """
    Resolve a config with all includes applied.

    Returns (resolved_config, resolution_graph).

    Raises:
        NotFoundError: If referenced config doesn't exist.
        CycleDetectedError: If a cycle is detected.
        UnprocessableError: If merge has type conflicts.
    """
    if visited is None:
        visited = set()

    # Check max depth
    if len(visited) > MAX_INCLUDE_CHAIN:
        raise UnprocessableError(
            'unprocessable',
            'Maximum include chain length exceeded',
            {'reason': 'max_depth'}
        )

    # Get the config version
    if version is None:
        config_version = storage.get_active(name, scope)
    else:
        config_version = storage.get_version(name, scope, version)

    # Create unique identifier for this config/version
    config_id = (name, hash(scope), config_version.version)

    # Check for cycles
    if config_id in visited:
        raise CycleDetectedError(
            'cycle_detected',
            f'Cycle detected involving {name}',
            {'cycle': [n.to_dict() for n in visited]}
        )

    visited = visited | {config_id}

    # Start with empty object
    resolved = {}
    graph = [ResolutionNode(
        name=config_version.name,
        scope=config_version.scope,
        version_used=config_version.version
    )]

    # Process includes in order
    for include_ref in config_version.includes:
        # Determine which version to use
        ref_version = include_ref.version
        if ref_version is None:
            # Use active version at resolution time
            ref_version = storage.get_active(include_ref.name, include_ref.scope).version

        # Recursively resolve
        included_config, included_graph = resolve_config(
            storage,
            include_ref.name,
            include_ref.scope,
            ref_version,
            visited
        )

        # Merge into accumulator
        try:
            resolved = deep_merge(resolved, included_config)
        except UnprocessableError as e:
            # Re-raise with more context
            raise UnprocessableError(
                'unprocessable',
                e.message,
                e.details
            )

        # Add to graph (without duplicates)
        for node in included_graph:
            node_id = (node.name, hash(node.scope), node.version_used)
            if node_id not in { (n.name, hash(n.scope), n.version_used) for n in graph }:
                graph.append(node)

    # Finally, merge own config on top
    try:
        resolved = deep_merge(resolved, config_version.config)
    except UnprocessableError as e:
        raise UnprocessableError(
            'unprocessable',
            e.message,
            e.details
        )

    return resolved, graph


# =============================================================================
# FastAPI Application
# =============================================================================

app = FastAPI(title='Config Service', version='1.0.0')
storage = ConfigStorage()


# =============================================================================
# Request/Response models
# =============================================================================

class ScopeModel(BaseModel):
    """Scope model for API."""

    root: dict[str, str] = Field(..., min_length=0)

    @field_validator('root')
    @classmethod
    def validate_scope(cls, v):
        if not isinstance(v, dict):
            raise ValueError('Scope must be an object')
        for k, val in v.items():
            if not isinstance(k, str):
                raise ValueError(f'Scope key must be string, got {type(k).__name__}')
            if not isinstance(val, str):
                raise ValueError(f'Scope value must be string, got {type(val).__name__}')
        return v


class IncludeRefModel(BaseModel):
    """Include reference model for API."""

    name: str = Field(..., min_length=1)
    scope: dict[str, str]
    version: int | None = None

    @field_validator('version')
    @classmethod
    def validate_version(cls, v):
        if v is not None and (not isinstance(v, int) or v < 1):
            raise ValueError('Version must be a positive integer')
        return v


class CreateConfigRequest(BaseModel):
    """Request for creating a config."""

    scope: dict[str, str]
    config: dict[str, Any]
    includes: list[dict[str, Any]] = []
    inherits_active: bool = False


class VersionListItem(BaseModel):
    """Version list item."""

    version: int
    active: bool


class VersionListResponse(BaseModel):
    """Response for listing versions."""

    name: str
    scope: dict[str, str]
    versions: list[VersionListItem]


class ConfigResponse(BaseModel):
    """Response for config details."""

    name: str
    scope: dict[str, str]
    version: int
    active: bool
    config: dict[str, Any]
    includes: list[dict[str, Any]]


class ActivateResponse(BaseModel):
    """Response for activate/rollback."""

    name: str
    scope: dict[str, str]
    version: int
    active: bool


class ResolveRequest(BaseModel):
    """Request for resolving a config."""

    scope: dict[str, str]
    version: int | None = None
    dry_run: bool = False


class ResolveResponse(BaseModel):
    """Response for resolving a config."""

    name: str
    scope: dict[str, str]
    version_used: int
    resolved_config: dict[str, Any]
    resolution_graph: list[dict[str, Any]]


class RollbackRequest(BaseModel):
    """Request for rollback."""

    scope: dict[str, str]
    to_version: int


class HealthResponse(BaseModel):
    """Health check response."""

    ok: bool


# =============================================================================
# Error response helper
# =============================================================================

def error_response(code: str, message: str, details: dict[str, Any] | None = None) -> JSONResponse:
    """Create a standardized error response."""
    status_map = {
        'invalid_input': 400,
        'not_found': 404,
        'conflict': 409,
        'cycle_detected': 409,
        'unprocessable': 422,
        'rate_limited': 429,
        'too_large': 413,
        'internal': 500,
    }
    status_code = status_map.get(code, 500)

    return JSONResponse(
        status_code=status_code,
        content=to_canonical_json({
            'error': {
                'code': code,
                'message': message,
                'details': details or {},
            }
        }),
        media_type='application/json; charset=utf-8'
    )


def to_canonical_json(obj: Any) -> str:
    """Convert object to canonical JSON string."""
    # Sort keys recursively
    def sort_keys(o):
        if isinstance(o, dict):
            return {k: sort_keys(v) for k, v in sorted(o.items())}
        elif isinstance(o, list):
            return [sort_keys(item) for item in o]
        return o

    sorted_obj = sort_keys(obj)
    # Use separators without spaces, ensure no trailing whitespace
    json_str = json.dumps(
        sorted_obj,
        separators=CANONICAL_SEPARATORS,
        ensure_ascii=False,
        default=lambda o: str(o) if isinstance(o, (set, tuple)) else o
    )
    # Add single newline at end
    return json_str + '\n'


# =============================================================================
# Exception handlers
# =============================================================================

@app.exception_handler(ConfigError)
async def config_error_handler(request: Request, exc: ConfigError):
    return error_response(exc.code, exc.message, exc.details)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    # Extract error details
    errors = exc.errors()
    details = {'errors': errors}
    return error_response('invalid_input', 'Validation failed', details)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 413:
        return error_response('too_large', 'Request body too large')
    return error_response('internal', str(exc_detail), {'status_code': exc.status_code})


# =============================================================================
# Middleware
# =============================================================================

@app.middleware('http')
async def request_size_limit(request: Request, call_next):
    """Middleware to enforce max request size."""
    content_length = request.headers.get('content-length')
    if content_length and int(content_length) > MAX_REQUEST_SIZE:
        return error_response('too_large', 'Request body exceeds 1 MiB limit')

    # Also check actual body size
    body = await request.body()
    if len(body) > MAX_REQUEST_SIZE:
        return error_response('too_large', 'Request body exceeds 1 MiB limit')

    # Restore body for subsequent reading
    request._body = body
    return await call_next(request)


# =============================================================================
# Endpoints
# =============================================================================

@app.get('/healthz')
async def healthcheck() -> HealthResponse:
    """Health check endpoint."""
    return HealthResponse(ok=True)


@app.post('/v1/configs/{name}')
async def create_config(request: Request, name: str):
    """Create a new version of a config."""

    body_bytes = await request.body()
    if len(body_bytes) > MAX_REQUEST_SIZE:
        return error_response('too_large', 'Request body too large')

    try:
        body_data = json.loads(body_bytes)
    except json.JSONDecodeError as e:
        return error_response('invalid_input', f'Invalid JSON: {e}')

    # Validate required fields
    if 'scope' not in body_data:
        return error_response('invalid_input', 'Missing required field: scope')
    if 'config' not in body_data:
        return error_response('invalid_input', 'Missing required field: config')

    try:
        scope = Scope.from_dict(body_data['scope'])
        config = body_data['config']
        if not isinstance(config, dict):
            return error_response('invalid_input', 'Config must be a JSON object')

        includes = []
        for inc_dict in body_data.get('includes', []):
            includes.append(IncludeRef.from_dict(inc_dict))

        inherits_active = body_data.get('inherits_active', False)
        if not isinstance(inherits_active, bool):
            return error_response('invalid_input', 'inherits_active must be a boolean')

        # Create the version
        new_version = storage.create_version(
            name=name,
            scope=scope,
            config=config,
            includes=includes,
            inherits_active=inherits_active
        )

        return JSONResponse(
            status_code=201,
            content=to_canonical_json({
                'name': new_version.name,
                'scope': new_version.scope.to_dict(),
                'version': new_version.version,
                'active': new_version.active,
            }),
            media_type='application/json; charset=utf-8'
        )
    except ConfigError as e:
        return error_response(e.code, e.message, e.details)


@app.post('/v1/configs/{name}:versions')
async def list_versions(request: Request, name: str):
    """List all versions for a (name, scope) pair."""

    body_bytes = await request.body()
    try:
        body_data = json.loads(body_bytes) if body_bytes else {}
    except json.JSONDecodeError as e:
        return error_response('invalid_input', f'Invalid JSON: {e}')

    if 'scope' not in body_data:
        return error_response('invalid_input', 'Missing required field: scope')

    try:
        scope = Scope.from_dict(body_data['scope'])
        versions = storage.list_versions(name, scope)

        # Sort by version (should already be sorted, but ensure it)
        versions.sort(key=lambda v: v.version)

        response = VersionListResponse(
            name=name,
            scope=scope.to_dict(),
            versions=[VersionListItem(version=v.version, active=v.active) for v in versions]
        )

        return JSONResponse(
            status_code=200,
            content=to_canonical_json(response.model_dump(exclude_unset=True)),
            media_type='application/json; charset=utf-8'
        )
    except ConfigError as e:
        return error_response(e.code, e.message, e.details)


@app.post('/v1/configs/{name}/{version}')
async def get_version(request: Request, name: str, version: int):
    """Get a specific raw version."""

    body_bytes = await request.body()
    try:
        body_data = json.loads(body_bytes) if body_bytes else {}
    except json.JSONDecodeError as e:
        return error_response('invalid_input', f'Invalid JSON: {e}')

    if 'scope' not in body_data:
        return error_response('invalid_input', 'Missing required field: scope')

    try:
        scope = Scope.from_dict(body_data['scope'])
        config_version = storage.get_version(name, scope, version)

        response = ConfigResponse(
            name=config_version.name,
            scope=config_version.scope.to_dict(),
            version=config_version.version,
            active=config_version.active,
            config=config_version.config,
            includes=[inc.to_dict() for inc in config_version.includes]
        )

        return JSONResponse(
            status_code=200,
            content=to_canonical_json(response.model_dump(exclude_unset=True)),
            media_type='application/json; charset=utf-8'
        )
    except ConfigError as e:
        return error_response(e.code, e.message, e.details)


@app.post('/v1/configs/{name}:active')
async def get_active(request: Request, name: str):
    """Get the active raw version."""

    body_bytes = await request.body()
    try:
        body_data = json.loads(body_bytes) if body_bytes else {}
    except json.JSONDecodeError as e:
        return error_response('invalid_input', f'Invalid JSON: {e}')

    if 'scope' not in body_data:
        return error_response('invalid_input', 'Missing required field: scope')

    try:
        scope = Scope.from_dict(body_data['scope'])
        config_version = storage.get_active(name, scope)

        response = ConfigResponse(
            name=config_version.name,
            scope=config_version.scope.to_dict(),
            version=config_version.version,
            active=config_version.active,
            config=config_version.config,
            includes=[inc.to_dict() for inc in config_version.includes]
        )

        return JSONResponse(
            status_code=200,
            content=to_canonical_json(response.model_dump(exclude_unset=True)),
            media_type='application/json; charset=utf-8'
        )
    except ConfigError as e:
        return error_response(e.code, e.message, e.details)


@app.post('/v1/configs/{name}/{version}:activate')
async def activate_version(request: Request, name: str, version: int):
    """Activate a specific version."""

    body_bytes = await request.body()
    try:
        body_data = json.loads(body_bytes) if body_bytes else {}
    except json.JSONDecodeError as e:
        return error_response('invalid_input', f'Invalid JSON: {e}')

    if 'scope' not in body_data:
        return error_response('invalid_input', 'Missing required field: scope')

    try:
        scope = Scope.from_dict(body_data['scope'])
        activated = storage.activate_version(name, scope, version)

        response = ActivateResponse(
            name=activated.name,
            scope=activated.scope.to_dict(),
            version=activated.version,
            active=activated.active
        )

        return JSONResponse(
            status_code=200,
            content=to_canonical_json(response.model_dump(exclude_unset=True)),
            media_type='application/json; charset=utf-8'
        )
    except ConfigError as e:
        return error_response(e.code, e.message, e.details)


@app.post('/v1/configs/{name}:rollback')
async def rollback(request: Request, name: str):
    """Rollback to an earlier version."""

    body_bytes = await request.body()
    try:
        body_data = json.loads(body_bytes) if body_bytes else {}
    except json.JSONDecodeError as e:
        return error_response('invalid_input', f'Invalid JSON: {e}')

    if 'scope' not in body_data:
        return error_response('invalid_input', 'Missing required field: scope')
    if 'to_version' not in body_data:
        return error_response('invalid_input', 'Missing required field: to_version')

    try:
        scope = Scope.from_dict(body_data['scope'])
        to_version = body_data['to_version']
        if not isinstance(to_version, int) or to_version < 1:
            return error_response('invalid_input', 'to_version must be a positive integer')

        rolled_back = storage.rollback(name, scope, to_version)

        response = ActivateResponse(
            name=rolled_back.name,
            scope=rolled_back.scope.to_dict(),
            version=rolled_back.version,
            active=rolled_back.active
        )

        return JSONResponse(
            status_code=200,
            content=to_canonical_json(response.model_dump(exclude_unset=True)),
            media_type='application/json; charset=utf-8'
        )
    except ConfigError as e:
        return error_response(e.code, e.message, e.details)


@app.post('/v1/configs/{name}:resolve')
async def resolve(request: Request, name: str):
    """Resolve a config with all imports applied."""

    body_bytes = await request.body()
    try:
        body_data = json.loads(body_bytes) if body_bytes else {}
    except json.JSONDecodeError as e:
        return error_response('invalid_input', f'Invalid JSON: {e}')

    if 'scope' not in body_data:
        return error_response('invalid_input', 'Missing required field: scope')

    try:
        scope = Scope.from_dict(body_data['scope'])
        version = body_data.get('version')
        dry_run = body_data.get('dry_run', False)

        if version is not None:
            if not isinstance(version, int) or version < 1:
                return error_response('invalid_input', 'Version must be a positive integer')

        if not isinstance(dry_run, bool):
            return error_response('invalid_input', 'dry_run must be a boolean')

        # Perform resolution
        resolved_config, resolution_graph = resolve_config(
            storage,
            name,
            scope,
            version
        )

        # Determine version_used
        if version is None:
            config_version = storage.get_active(name, scope)
            version_used = config_version.version
        else:
            version_used = version

        response = ResolveResponse(
            name=name,
            scope=scope.to_dict(),
            version_used=version_used,
            resolved_config=resolved_config,
            resolution_graph=[node.to_dict() for node in resolution_graph]
        )

        return JSONResponse(
            status_code=200,
            content=to_canonical_json(response.model_dump(exclude_unset=True)),
            media_type='application/json; charset=utf-8'
        )
    except ConfigError as e:
        return error_response(e.code, e.message, e.details)


# =============================================================================
# Main entry point
# =============================================================================

def main():
    import argparse

    parser = argparse.ArgumentParser(description='Config Service')
    parser.add_argument('--address', default='0.0.0.0', help='Address to bind to')
    parser.add_argument('--port', type=int, default=8080, help='Port to listen on')
    args = parser.parse_args()

    uvicorn.run(
        'config_server:app',
        host=args.address,
        port=args.port,
        log_level='info'
    )


if __name__ == '__main__':
    main()
