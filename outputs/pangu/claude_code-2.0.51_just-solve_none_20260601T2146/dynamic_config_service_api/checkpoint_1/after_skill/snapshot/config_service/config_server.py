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


Scope = dict[str, str]

def validate_scope(d: dict[str, str]) -> dict[str, str]:
    """Validate scope dict - returns the dict if valid."""
    for k, v in d.items():
        if not isinstance(k, str):
            raise ConfigError('invalid_input', f'Scope key must be string, got {type(k).__name__}')
        if not isinstance(v, str):
            raise ConfigError('invalid_input', f'Scope value must be string, got {type(v).__name__}')
    return d

def scope_hash(scope: Scope) -> int:
    """Hash a scope for dictionary lookup."""
    return hash(tuple(sorted(scope.items())))


@dataclass(frozen=True)
class IncludeRef:
    """Reference to another config."""

    name: str
    scope: Scope
    version: int | None  # None means use current active

    def to_dict(self) -> dict[str, Any]:
        result = {
            'name': self.name,
            'scope': self.scope,
        }
        if self.version is not None:
            result['version'] = self.version
        return result

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> 'IncludeRef':
        if not isinstance(d, dict):
            raise ConfigError('invalid_input', 'Include reference must be a JSON object')

        if 'name' not in d:
            raise ConfigError('invalid_input', 'Include reference must have "name"')
        if 'scope' not in d:
            raise ConfigError('invalid_input', 'Include reference must have "scope"')

        name = d['name']
        if not isinstance(name, str) or not name:
            raise ConfigError('invalid_input', 'Include name must be non-empty string')

        scope = validate_scope(d['scope'])
        version = d.get('version')
        if version is not None:
            if not isinstance(version, int) or version < 1:
                raise ConfigError('invalid_input', 'Version must be positive integer')

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
            'scope': self.scope,
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
        return (name, scope_hash(scope))

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
            raise ConfigError('conflict', f'Maximum {MAX_VERSIONS_PER_SCOPE} versions reached for {name}')

        # Determine next version number
        next_version = len(versions) + 1

        # Check idempotency - if identical request, return existing
        for v in versions:
            if v.config == config and v.includes == tuple(includes):
                # Return existing version, but ensure it's active
                if not v.active:
                    object.__setattr__(v, 'active', True)
                return v

        # If inherits_active is True, inherit omitted fields from active version
        if inherits_active and versions:
            active = versions[-1]  # Last one is active
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

        self._configs[key] = versions + [new_version]
        return new_version

    def get_version(self, name: str, scope: Scope, version: int) -> ConfigVersion:
        """Get a specific version."""
        versions = self._get_versions(name, scope)
        for v in versions:
            if v.version == version:
                return v
        raise ConfigError('not_found', f'Version {version} not found for {name}')

    def get_active(self, name: str, scope: Scope) -> ConfigVersion:
        """Get the active version."""
        versions = self._get_versions(name, scope)
        for v in reversed(versions):
            if v.active:
                return v
        raise ConfigError('not_found', f'No active version for {name}')

    def list_versions(self, name: str, scope: Scope) -> list[ConfigVersion]:
        """List all versions for a (name, scope) pair."""
        return list(self._get_versions(name, scope))

    def activate_version(self, name: str, scope: Scope, version: int) -> ConfigVersion:
        """Activate a specific version."""
        versions = self._get_versions(name, scope)
        for v in versions:
            if v.version == version:
                for ver in versions:
                    object.__setattr__(ver, 'active', ver.version == version)
                return v
        raise ConfigError('not_found', f'Version {version} not found for {name}')

    def rollback(self, name: str, scope: Scope, to_version: int) -> ConfigVersion:
        """Rollback to an earlier version."""
        versions = self._get_versions(name, scope)
        target = None
        for v in versions:
            if v.version == to_version:
                target = v
                break
        if target is None:
            raise ConfigError('not_found', f'Version {to_version} not found for {name}')

        current_active = None
        for v in reversed(versions):
            if v.active:
                current_active = v
                break
        if current_active is None:
            raise ConfigError('not_found', f'No active version for {name}')

        if to_version > current_active.version:
            raise ConfigError('conflict', f'Cannot rollback to version {to_version} (newer than active {current_active.version})')

        return self.activate_version(name, scope, to_version)


# =============================================================================
# Deep merge with type checking
# =============================================================================

def deep_merge(base: dict[str, Any], override: dict[str, Any], path: str = '') -> dict[str, Any]:
    """Deep merge two dictionaries. Override values take precedence."""
    result = dict(base)

    for key, value in override.items():
        current_path = f'{path}/{key}' if path else f'/{key}'

        if key in result:
            existing = result[key]

            if isinstance(existing, dict) and isinstance(value, dict):
                result[key] = deep_merge(existing, value, current_path)
            elif isinstance(existing, list) and isinstance(value, list):
                result[key] = value
            elif isinstance(existing, (str, int, float, bool, type(None))) and \
                 isinstance(value, (str, int, float, bool, type(None))):
                result[key] = value
            else:
                raise ConfigError(
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
            'scope': self.scope,
            'version_used': self.version_used,
        }


def resolve_config(
    storage: ConfigStorage,
    name: str,
    scope: Scope,
    version: int | None,
    visited: set[tuple[str, int, int]] | None = None
) -> tuple[dict[str, Any], list[ResolutionNode]]:
    """Resolve a config with all includes applied."""
    if visited is None:
        visited = set()

    if len(visited) > MAX_INCLUDE_CHAIN:
        raise ConfigError('unprocessable', 'Maximum include chain length exceeded', {'reason': 'max_depth'})

    if version is None:
        config_version = storage.get_active(name, scope)
    else:
        config_version = storage.get_version(name, scope, version)

    config_id = (name, hash(scope), config_version.version)

    if config_id in visited:
        raise ConfigError('cycle_detected', f'Cycle detected involving {name}', {'cycle': [n.to_dict() for n in visited]})

    visited = visited | {config_id}

    resolved = {}
    graph = [ResolutionNode(name=config_version.name, scope=config_version.scope, version_used=config_version.version)]

    for include_ref in config_version.includes:
        ref_version = include_ref.version
        if ref_version is None:
            ref_version = storage.get_active(include_ref.name, include_ref.scope).version

        included_config, included_graph = resolve_config(storage, include_ref.name, include_ref.scope, ref_version, visited)

        try:
            resolved = deep_merge(resolved, included_config)
        except ConfigError as e:
            if e.code == 'unprocessable':
                raise
            raise

        for node in included_graph:
            node_id = (node.name, hash(node.scope), node.version_used)
            if node_id not in {(n.name, hash(n.scope), n.version_used) for n in graph}:
                graph.append(node)

    try:
        resolved = deep_merge(resolved, config_version.config)
    except ConfigError:
        raise

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
# Request helpers
# =============================================================================

async def parse_body(request: Request) -> dict[str, Any]:
    """Parse and validate request body."""
    body_bytes = await request.body()
    if len(body_bytes) > MAX_REQUEST_SIZE:
        raise ConfigError('too_large', 'Request body too large')
    try:
        return json.loads(body_bytes)
    except json.JSONDecodeError as e:
        raise ConfigError('invalid_input', f'Invalid JSON: {e}')


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
    body_data = await parse_body(request)

    if 'scope' not in body_data or 'config' not in body_data:
        raise ConfigError('invalid_input', 'Missing required field: scope or config')

    config = body_data['config']
    if not isinstance(config, dict):
        raise ConfigError('invalid_input', 'Config must be a JSON object')

    includes = [IncludeRef.from_dict(inc_dict) for inc_dict in body_data.get('includes', [])]
    inherits_active = body_data.get('inherits_active', False)
    if not isinstance(inherits_active, bool):
        raise ConfigError('invalid_input', 'inherits_active must be a boolean')

    new_version = storage.create_version(name, validate_scope(body_data['scope']), config, includes, inherits_active)

    return JSONResponse(
        status_code=201,
        content=to_canonical_json({
            'name': new_version.name,
            'scope': new_version.scope,
            'version': new_version.version,
            'active': new_version.active,
        }),
        media_type='application/json; charset=utf-8'
    )


@app.post('/v1/configs/{name}:versions')
async def list_versions(request: Request, name: str):
    """List all versions for a (name, scope) pair."""
    body_data = await parse_body(request)

    if 'scope' not in body_data:
        raise ConfigError('invalid_input', 'Missing required field: scope')

    scope = validate_scope(body_data['scope'])
    versions = storage.list_versions(name, scope)

    versions.sort(key=lambda v: v.version)
    return JSONResponse(
        status_code=200,
        content=to_canonical_json({
            'name': name,
            'scope': scope,
            'versions': [{'version': v.version, 'active': v.active} for v in versions]
        }),
        media_type='application/json; charset=utf-8'
    )


@app.post('/v1/configs/{name}/{version}')
async def get_version(request: Request, name: str, version: int):
    """Get a specific raw version."""
    body_data = await parse_body(request)

    if 'scope' not in body_data:
        raise ConfigError('invalid_input', 'Missing required field: scope')

    config_version = storage.get_version(name, validate_scope(body_data['scope']), version)

    return JSONResponse(
        status_code=200,
        content=to_canonical_json({
            'name': config_version.name,
            'scope': config_version.scope,
            'version': config_version.version,
            'active': config_version.active,
            'config': config_version.config,
            'includes': [inc.to_dict() for inc in config_version.includes]
        }),
        media_type='application/json; charset=utf-8'
    )


@app.post('/v1/configs/{name}:active')
async def get_active(request: Request, name: str):
    """Get the active raw version."""
    body_data = await parse_body(request)

    if 'scope' not in body_data:
        raise ConfigError('invalid_input', 'Missing required field: scope')

    config_version = storage.get_active(name, validate_scope(body_data['scope']))

    return JSONResponse(
        status_code=200,
        content=to_canonical_json({
            'name': config_version.name,
            'scope': config_version.scope,
            'version': config_version.version,
            'active': config_version.active,
            'config': config_version.config,
            'includes': [inc.to_dict() for inc in config_version.includes]
        }),
        media_type='application/json; charset=utf-8'
    )


@app.post('/v1/configs/{name}/{version}:activate')
async def activate_version(request: Request, name: str, version: int):
    """Activate a specific version."""
    body_data = await parse_body(request)

    if 'scope' not in body_data:
        raise ConfigError('invalid_input', 'Missing required field: scope')

    activated = storage.activate_version(name, validate_scope(body_data['scope']), version)

    return JSONResponse(
        status_code=200,
        content=to_canonical_json({
            'name': activated.name,
            'scope': activated.scope,
            'version': activated.version,
            'active': activated.active
        }),
        media_type='application/json; charset=utf-8'
    )


@app.post('/v1/configs/{name}:rollback')
async def rollback(request: Request, name: str):
    """Rollback to an earlier version."""
    body_data = await parse_body(request)

    if 'scope' not in body_data:
        raise ConfigError('invalid_input', 'Missing required field: scope')
    if 'to_version' not in body_data:
        raise ConfigError('invalid_input', 'Missing required field: to_version')

    to_version = body_data['to_version']
    if not isinstance(to_version, int) or to_version < 1:
        raise ConfigError('invalid_input', 'to_version must be a positive integer')

    rolled_back = storage.rollback(name, validate_scope(body_data['scope']), to_version)

    return JSONResponse(
        status_code=200,
        content=to_canonical_json({
            'name': rolled_back.name,
            'scope': rolled_back.scope,
            'version': rolled_back.version,
            'active': rolled_back.active
        }),
        media_type='application/json; charset=utf-8'
    )


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
