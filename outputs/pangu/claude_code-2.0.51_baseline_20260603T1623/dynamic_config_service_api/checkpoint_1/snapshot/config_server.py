#!/usr/bin/env python3
"""
Configuration Server with Immutable Versions, Scoping, Rollback, and Imports
"""

import json
from typing import Any

from flask import Flask, request, Response

app = Flask(__name__)

# In-memory storage
configs: dict[tuple[str, frozenset], list["ConfigVersion"]] = {}
active_version_cache: dict[tuple[str, frozenset], int] = {}

MAXRequestBody = 1024 * 1024  # 1 MiB
MAX_DEPTH = 64
MAX_VERSIONS = 10_000


class ConfigVersion:
    __slots__ = ('version', 'config', 'includes', 'active', 'canonical_body')

    def __init__(self, version: int, config: dict, includes: list[dict],
                 active: bool = False, canonical_body: str = None):
        self.version = version
        self.config = config
        self.includes = includes
        self.active = active
        self.canonical_body = canonical_body


def normalize_scope(scope: dict) -> frozenset:
    """Normalize scope dict to frozenset for use as dict key."""
    return frozenset((k, v) for k, v in sorted(scope.items()))


def denormalize_scope(scope_fs: frozenset) -> dict:
    """Convert frozenset back to dict."""
    return dict(sorted(scope_fs))


def error_response(code: str, message: str, details: dict = None,
                   status: int = 400) -> Response:
    """Generate standardized error response."""
    body = {
        "error": {
            "code": code,
            "message": message,
            "details": details or {}
        }
    }
    return Response(
        response=json.dumps(body, sort_keys=True, ensure_ascii=False) + '\n',
        status=status,
        mimetype='application/json; charset=utf-8'
    )


def success_response(data: dict, status: int = 200) -> Response:
    """Generate successful response with canonical JSON."""
    return Response(
        response=json.dumps(data, sort_keys=True, ensure_ascii=False) + '\n',
        status=status,
        mimetype='application/json; charset=utf-8'
    )


def validate_non_empty_string(value: Any, field: str) -> str:
    """Validate that value is a non-empty string."""
    if not isinstance(value, str) or len(value) == 0:
        raise ValueError(f'{field} must be a non-empty string')
    return value


def validate_scope(scope: Any) -> dict:
    """Validate scope is a dict of string->string."""
    if not isinstance(scope, dict):
        raise ValueError('scope must be an object')
    for k, v in scope.items():
        if not isinstance(k, str):
            raise ValueError(f'scope key {k!r} must be a string')
        if not isinstance(v, str):
            raise ValueError(f'scope value for key {k!r} must be a string')
    return scope


def validate_include_ref(ref: Any) -> dict:
    """Validate an include reference."""
    if not isinstance(ref, dict):
        raise ValueError('include reference must be an object')

    name = ref.get('name')
    validate_non_empty_string(name, 'include name')

    scope = ref.get('scope')
    validate_scope(scope)

    version = ref.get('version')
    if version is not None and version is not False:
        if not isinstance(version, int) or version < 1:
            raise ValueError('include version must be a positive integer or null')

    return {
        'name': name,
        'scope': scope,
        'version': version
    }


def deep_merge(base: dict, override: dict, path: str = '') -> dict:
    """
    Deep-merge override into base.
    Returns new dict.
    Raises ValueError on type conflicts.
    """
    result = dict(base)

    for key, value in override.items():
        current_path = f'{path}/{key}' if path else f'/{key}'

        if key in result:
            existing = result[key]

            # Type conflict detection
            if isinstance(existing, dict) and isinstance(value, dict):
                result[key] = deep_merge(existing, value, current_path)
            elif isinstance(existing, list) or isinstance(value, list):
                # Arrays replace entirely
                result[key] = value if isinstance(value, list) else value
            else:
                # Scalars: replace
                if (type(existing) != type(value) and
                        existing is not None and value is not None):
                    raise ValueError(f'Type conflict at {current_path}: '
                                   f'{type(existing).__name__} vs {type(value).__name__}')
                result[key] = value
        else:
            result[key] = value

    return result


def resolve_config(name: str, scope: dict, version: int | None = None,
                   visited: set | None = None) -> tuple[dict, list[dict]]:
    """
    Resolve a config with all includes applied.
    Returns (resolved_config, resolution_graph).
    """
    scope_fs = normalize_scope(scope)
    key = (name, scope_fs)

    if visited is None:
        visited = set()

    # Check cycle using (name, scope_fs, version) triplet
    visit_key = (name, scope_fs, version)
    if visit_key in visited:
        raise ValueError('cycle_detected')

    if len(visited) > MAX_DEPTH:
        raise ValueError('max_depth')

    # Get the config version
    if key not in configs or not configs[key]:
        raise ValueError('not_found')

    versions = configs[key]

    # Determine which version to use
    if version is None:
        # Use active version
        if key not in active_version_cache:
            raise ValueError('not_found')
        target_version = active_version_cache[key]
    else:
        # Find specific version
        target_version = None
        for v in versions:
            if v.version == version:
                target_version = version
                break
        if target_version is None:
            raise ValueError('not_found')

    # Find the version object
    target_config = None
    for v in versions:
        if v.version == target_version:
            target_config = v
            break

    if target_config is None:
        raise ValueError('not_found')

    # Mark visited with the triplet
    new_visited = visited | {visit_key}

    # Start with empty object and process includes
    resolved = {}
    graph = [{
        'name': name,
        'scope': denormalize_scope(scope_fs),
        'version_used': target_version
    }]

    # Process includes in order
    for include_ref in target_config.includes:
        inc_name = include_ref['name']
        inc_scope = include_ref['scope']
        inc_version = include_ref['version']

        inc_resolved, inc_graph = resolve_config(inc_name, inc_scope,
                                                  inc_version, new_visited)

        try:
            resolved = deep_merge(resolved, inc_resolved)
        except ValueError as e:
            if 'Type conflict' in str(e):
                path_start = str(e).find('at ') + 3
                path_end = str(e).find(':', path_start)
                if path_end > path_start:
                    conflict_path = str(e)[path_start:path_end]
                else:
                    conflict_path = '/'
                raise ValueError(f'Type conflict at {conflict_path}')
            raise

        # Add to graph (deduplicate by name+scope+version)
        for node in inc_graph:
            node_key = (node['name'], frozenset(node['scope'].items()),
                       node['version_used'])
            if node_key not in new_visited:
                graph.append(node)
                new_visited |= {node_key}

    # Finally merge own config on top
    try:
        resolved = deep_merge(resolved, target_config.config)
    except ValueError as e:
        if 'Type conflict' in str(e):
            path_start = str(e).find('at ') + 3
            path_end = str(e).find(':', path_start)
            if path_end > path_start:
                conflict_path = str(e)[path_start:path_end]
            else:
                conflict_path = '/'
            raise ValueError(f'Type conflict at {conflict_path}')
        raise

    return resolved, graph


@app.route('/healthz', methods=['GET'])
def healthz():
    """Health check endpoint."""
    return success_response({'ok': True})


@app.route('/v1/configs/<name>', methods=['POST'])
def create_config(name: str):
    """Create a new version of a config."""
    # Check content type (accepts with or without charset)
    ct = request.content_type
    if not ct:
        return error_response(
            'invalid_input',
            'Content-Type must be application/json; charset=utf-8',
            status=415
        )
    # Allow both with and without charset specification
    main_type = ct.split(';')[0].strip()
    if main_type != 'application/json':
        return error_response(
            'invalid_input',
            'Content-Type must be application/json; charset=utf-8',
            status=415
        )

    # Check body size
    if request.content_length and request.content_length > MAXRequestBody:
        return error_response(
            'too_large',
            'Request body exceeds 1 MiB limit',
            status=413
        )

    # Parse JSON
    try:
        body = request.get_json(silent=False, force=True)
    except Exception:
        return error_response(
            'invalid_input',
            'Invalid JSON in request body',
            status=400
        )

    if not isinstance(body, dict):
        return error_response(
            'invalid_input',
            'Request body must be a JSON object',
            status=400
        )

    # Validate required fields
    try:
        scope = validate_scope(body.get('scope'))
        config = body.get('config')
        if not isinstance(config, dict):
            return error_response(
                'invalid_input',
                'config must be an object',
                status=400
            )
        includes_raw = body.get('includes', [])
        if not isinstance(includes_raw, list):
            return error_response(
                'invalid_input',
                'includes must be an array',
                status=400
            )
        includes = [validate_include_ref(ref) for ref in includes_raw]
        inherits_active = body.get('inherits_active', False)
        if not isinstance(inherits_active, bool):
            return error_response(
                'invalid_input',
                'inherits_active must be a boolean',
                status=400
            )
    except ValueError as e:
        return error_response('invalid_input', str(e), status=400)

    scope_fs = normalize_scope(scope)
    key = (name, scope_fs)

    # Canonicalize body for idempotency check
    canonical_body = json.dumps(body, sort_keys=True, ensure_ascii=False)

    # Check idempotency - exact body match
    existing_versions = configs.get(key, [])
    for v in existing_versions:
        if v.canonical_body == canonical_body:
            # Return existing version info
            return success_response({
                'name': name,
                'scope': scope,
                'version': v.version,
                'active': v.active
            }, status=201)

    # Get next version number
    if key not in configs:
        configs[key] = []
        next_version = 1
    else:
        if len(configs[key]) >= MAX_VERSIONS:
            return error_response(
                'conflict',
                f'Maximum {MAX_VERSIONS} versions exceeded',
                status=409
            )
        next_version = max(v.version for v in configs[key]) + 1

    # Handle inherits_active
    if inherits_active:
        active_version = active_version_cache.get(key)
        if active_version is not None:
            # Find the active version's config
            for v in existing_versions:
                if v.version == active_version and v.active:
                    # Merge: config from active for keys not present in new config
                    for k, val in v.config.items():
                        if k not in config:
                            config[k] = val
                    # For includes: if no includes specified, inherit active's includes
                    if not includes_raw:
                        includes = v.includes[:]
                    break

    # Create new version
    new_version = ConfigVersion(
        version=next_version,
        config=config,
        includes=includes,
        active=True,  # New configs are active by default
        canonical_body=canonical_body
    )

    # Deactivate all previous versions
    for v in configs[key]:
        v.active = False

    configs[key].append(new_version)
    active_version_cache[key] = next_version

    return success_response({
        'name': name,
        'scope': scope,
        'version': next_version,
        'active': True
    }, status=201)


@app.route('/v1/configs/<name>:versions', methods=['POST'])
def list_versions(name: str):
    """List all versions for a (name, scope)."""
    ct = request.content_type
    if not ct:
        return error_response(
            'invalid_input',
            'Content-Type must be application/json; charset=utf-8',
            status=415
        )
    # Allow both with and without charset specification
    main_type = ct.split(';')[0].strip()
    if main_type != 'application/json':
        return error_response(
            'invalid_input',
            'Content-Type must be application/json; charset=utf-8',
            status=415
        )

    try:
        body = request.get_json(silent=False, force=True)
        scope = validate_scope(body.get('scope'))
    except ValueError as e:
        return error_response('invalid_input', str(e), status=400)
    except Exception:
        return error_response(
            'invalid_input',
            'Invalid JSON in request body',
            status=400
        )

    scope_fs = normalize_scope(scope)
    key = (name, scope_fs)

    if key not in configs or not configs[key]:
        return error_response(
            'not_found',
            f'No configs found for name={name}, scope={scope}',
            status=404
        )

    versions_list = sorted([
        {'version': v.version, 'active': v.active}
        for v in configs[key]
    ], key=lambda x: x['version'])

    return success_response({
        'name': name,
        'scope': scope,
        'versions': versions_list
    })


@app.route('/v1/configs/<name>/<int:version>', methods=['POST'])
def get_version(name: str, version: int):
    """Get a specific raw version."""
    ct = request.content_type
    if not ct:
        return error_response(
            'invalid_input',
            'Content-Type must be application/json; charset=utf-8',
            status=415
        )
    # Allow both with and without charset specification
    main_type = ct.split(';')[0].strip()
    if main_type != 'application/json':
        return error_response(
            'invalid_input',
            'Content-Type must be application/json; charset=utf-8',
            status=415
        )

    try:
        body = request.get_json(silent=False, force=True)
        scope = validate_scope(body.get('scope'))
    except ValueError as e:
        return error_response('invalid_input', str(e), status=400)
    except Exception:
        return error_response(
            'invalid_input',
            'Invalid JSON in request body',
            status=400
        )

    scope_fs = normalize_scope(scope)
    key = (name, scope_fs)

    if key not in configs or not configs[key]:
        return error_response(
            'not_found',
            f'No configs found for name={name}, scope={scope}',
            status=404
        )

    for v in configs[key]:
        if v.version == version:
            return success_response({
                'name': name,
                'scope': scope,
                'version': v.version,
                'active': v.active,
                'config': v.config,
                'includes': v.includes
            })

    return error_response(
        'not_found',
        f'Version {version} not found for name={name}, scope={scope}',
        status=404
    )


@app.route('/v1/configs/<name>:active', methods=['POST'])
def get_active(name: str):
    """Get the active raw version."""
    ct = request.content_type
    if not ct:
        return error_response(
            'invalid_input',
            'Content-Type must be application/json; charset=utf-8',
            status=415
        )
    # Allow both with and without charset specification
    main_type = ct.split(';')[0].strip()
    if main_type != 'application/json':
        return error_response(
            'invalid_input',
            'Content-Type must be application/json; charset=utf-8',
            status=415
        )

    try:
        body = request.get_json(silent=False, force=True)
        scope = validate_scope(body.get('scope'))
    except ValueError as e:
        return error_response('invalid_input', str(e), status=400)
    except Exception:
        return error_response(
            'invalid_input',
            'Invalid JSON in request body',
            status=400
        )

    scope_fs = normalize_scope(scope)
    key = (name, scope_fs)

    if key not in active_version_cache:
        return error_response(
            'not_found',
            f'No active config found for name={name}, scope={scope}',
            status=404
        )

    active_ver = active_version_cache[key]
    for v in configs[key]:
        if v.version == active_ver:
            return success_response({
                'name': name,
                'scope': scope,
                'version': v.version,
                'active': True,
                'config': v.config,
                'includes': v.includes
            })

    return error_response(
        'not_found',
        f'Active version {active_ver} not found',
        status=404
    )


@app.route('/v1/configs/<name>/<int:version>:activate', methods=['POST'])
def activate_version(name: str, version: int):
    """Activate a specific version."""
    ct = request.content_type
    if not ct:
        return error_response(
            'invalid_input',
            'Content-Type must be application/json; charset=utf-8',
            status=415
        )
    # Allow both with and without charset specification
    main_type = ct.split(';')[0].strip()
    if main_type != 'application/json':
        return error_response(
            'invalid_input',
            'Content-Type must be application/json; charset=utf-8',
            status=415
        )

    try:
        body = request.get_json(silent=False, force=True)
        scope = validate_scope(body.get('scope'))
    except ValueError as e:
        return error_response('invalid_input', str(e), status=400)
    except Exception:
        return error_response(
            'invalid_input',
            'Invalid JSON in request body',
            status=400
        )

    scope_fs = normalize_scope(scope)
    key = (name, scope_fs)

    # Check if version exists for this pair
    version_found = False
    for v in configs.get(key, []):
        if v.version == version:
            version_found = True
            break

    if not version_found:
        return error_response(
            'conflict',
            f'Version {version} does not exist for name={name}, scope={scope}',
            status=409
        )

    # Activate this version, deactivate others
    for v in configs[key]:
        v.active = (v.version == version)

    active_version_cache[key] = version

    return success_response({
        'name': name,
        'scope': scope,
        'version': version,
        'active': True
    })


@app.route('/v1/configs/<name>:rollback', methods=['POST'])
def rollback(name: str):
    """Rollback to an earlier version."""
    ct = request.content_type
    if not ct:
        return error_response(
            'invalid_input',
            'Content-Type must be application/json; charset=utf-8',
            status=415
        )
    # Allow both with and without charset specification
    main_type = ct.split(';')[0].strip()
    if main_type != 'application/json':
        return error_response(
            'invalid_input',
            'Content-Type must be application/json; charset=utf-8',
            status=415
        )

    try:
        body = request.get_json(silent=False, force=True)
        scope = validate_scope(body.get('scope'))
        to_version = body.get('to_version')
        if not isinstance(to_version, int) or to_version < 1:
            return error_response(
                'invalid_input',
                'to_version must be a positive integer',
                status=400
            )
    except ValueError as e:
        return error_response('invalid_input', str(e), status=400)
    except Exception:
        return error_response(
            'invalid_input',
            'Invalid JSON in request body',
            status=400
        )

    scope_fs = normalize_scope(scope)
    key = (name, scope_fs)

    # Get current active version
    current_active = active_version_cache.get(key)
    if current_active is not None and to_version >= current_active and to_version != current_active:
        # Only allow rolling back to earlier versions, or idempotent re-activate same version
        return error_response(
            'conflict',
            'to_version must be earlier than current active version',
            status=409
        )

    # Check version exists
    version_found = False
    for v in configs.get(key, []):
        if v.version == to_version:
            version_found = True
            break

    if not version_found:
        return error_response(
            'conflict',
            f'Version {to_version} does not exist for name={name}, scope={scope}',
            status=409
        )

    # Activate the target version
    for v in configs[key]:
        v.active = (v.version == to_version)

    active_version_cache[key] = to_version

    return success_response({
        'name': name,
        'scope': scope,
        'version': to_version,
        'active': True
    })


@app.route('/v1/configs/<name>:resolve', methods=['POST'])
def resolve(name: str):
    """Resolve config with all imports applied."""
    ct = request.content_type
    if not ct:
        return error_response(
            'invalid_input',
            'Content-Type must be application/json; charset=utf-8',
            status=415
        )
    # Allow both with and without charset specification
    main_type = ct.split(';')[0].strip()
    if main_type != 'application/json':
        return error_response(
            'invalid_input',
            'Content-Type must be application/json; charset=utf-8',
            status=415
        )

    try:
        body = request.get_json(silent=False, force=True)
        scope = validate_scope(body.get('scope'))
        version = body.get('version')
        if version is not None and version is not False:
            if not isinstance(version, int) or version < 1:
                return error_response(
                    'invalid_input',
                    'version must be a positive integer or null',
                    status=400
                )
        dry_run = body.get('dry_run', False)
        if not isinstance(dry_run, bool):
            return error_response(
                'invalid_input',
                'dry_run must be a boolean',
                status=400
            )
    except ValueError as e:
        return error_response('invalid_input', str(e), status=400)
    except Exception:
        return error_response(
            'invalid_input',
            'Invalid JSON in request body',
            status=400
        )

    scope_fs = normalize_scope(scope)
    key = (name, scope_fs)

    try:
        # Determine version to resolve
        target_version = version

        if dry_run:
            if target_version is None:
                return error_response(
                    'invalid_input',
                    'dry_run requires version to be specified',
                    status=400
                )
            version_exists = False
            if key in configs:
                for v in configs[key]:
                    if v.version == target_version:
                        version_exists = True
                        break
            if not version_exists:
                return error_response(
                    'not_found',
                    f'Version {target_version} not found for name={name}, scope={scope}',
                    status=404
                )

        resolved_config, resolution_graph = resolve_config(
            name, scope, target_version
        )

        return success_response({
            'name': name,
            'scope': scope,
            'version_used': target_version if target_version
                            else active_version_cache.get(key),
            'resolved_config': resolved_config,
            'resolution_graph': resolution_graph
        })

    except ValueError as e:
        error_msg = str(e)
        if error_msg == 'cycle_detected':
            return error_response(
                'cycle_detected',
                'Cycle detected in include references',
                details={'reason': 'cycle_detected'},
                status=409
            )
        elif error_msg == 'max_depth':
            return error_response(
                'unprocessable',
                'Maximum include chain length exceeded',
                details={'reason': 'max_depth'},
                status=422
            )
        elif 'Type conflict' in error_msg:
            path = error_msg.replace('Type conflict at ', '')
            return error_response(
                'unprocessable',
                f'Type conflict at {path}',
                details={'path': path, 'reason': 'type_conflict'},
                status=422
            )
        else:
            return error_response('not_found', error_msg, status=404)


def main():
    import argparse

    parser = argparse.ArgumentParser(description='Configuration Server')
    parser.add_argument('--address', default='0.0.0.0',
                        help='Address to bind to')
    parser.add_argument('--port', type=int, default=8080,
                        help='Port to listen on')

    args = parser.parse_args()

    app.run(host=args.address, port=args.port, debug=False)


if __name__ == '__main__':
    main()
