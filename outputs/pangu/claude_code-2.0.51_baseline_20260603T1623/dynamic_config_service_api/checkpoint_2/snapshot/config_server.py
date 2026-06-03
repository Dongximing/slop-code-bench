#!/usr/bin/env python3
"""
Configuration Server with Immutable Versions, Scoping, Rollback, and Imports
"""

import datetime
import json
from typing import Any

from flask import Flask, request, Response

app = Flask(__name__)

# In-memory storage
configs: dict[tuple[str, frozenset], list["ConfigVersion"]] = {}
active_version_cache: dict[tuple[str, frozenset], int] = {}

# Schema registry storage
schemas: dict[tuple[str, int], dict] = {}  # (name, version) -> schema dict
schema_versions: dict[str, list[int]] = {}  # name -> sorted list of versions
schema_bindings: dict[tuple[str, frozenset], dict] = {}  # (name, scope) -> binding info

MAXRequestBody = 1024 * 1024  # 1 MiB
MAX_DEPTH = 64
MAX_VERSIONS = 10_000
MAX_SCHEMA_VERSIONS = 1_000
MAX_RAW_SIZE = 1024 * 1024  # 1 MiB


class ConfigVersion:
    __slots__ = ('version', 'config', 'includes', 'active', 'canonical_body')

    def __init__(self, version: int, config: dict, includes: list[dict],
                 active: bool = False, canonical_body: str = None,
                 schema_ref: dict = None):
        self.version = version
        self.config = config
        self.includes = includes
        self.active = active
        self.canonical_body = canonical_body
        self.schema_ref = schema_ref


import json
import yaml
import toml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError


def parse_raw_config(raw: str, fmt: str) -> dict:
    """Parse raw config string into canonical JSON object."""
    if fmt == 'json':
        # Standard RFC 8259. Reject trailing commas, comments
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f'Invalid JSON: {e}')
        if not isinstance(parsed, dict):
            raise ValueError('Config must be a JSON object')
        # Canonicalize
        canonical = json.loads(json.dumps(parsed, sort_keys=True, ensure_ascii=False))
        return canonical

    elif fmt == 'yaml':
        # YAML 1.2, no anchors/aliases, no custom tags, no merge keys
        # PyYAML's load() with Loader=yaml.SafeLoader disallows these by default
        # but we also need to ensure mapping keys are strings
        class RestrictedLoader(yaml.SafeLoader):
            pass

        # Disallow specific tags
        def _disallow_tag(loader, tag_suffix):
            raise ValueError('yaml_feature_not_allowed')

        for tag in ['tag', 'merge', '']:
            RestrictedLoader.add_constructor(tag, _disallow_tag)

        try:
            parsed = yaml.load(raw, Loader=RestrictedLoader)
        except ValueError as e:
            if 'yaml_feature_not_allowed' in str(e):
                raise
            raise ValueError(f'Invalid YAML: {e}')
        except Exception as e:
            raise ValueError(f'Invalid YAML: {e}')

        if not isinstance(parsed, dict):
            raise ValueError('Config must be a YAML mapping (object)')

        # Check all keys are strings
        for k in parsed.keys():
            if not isinstance(k, str):
                raise ValueError('yaml_feature_not_allowed')

        # Canonicalize
        canonical = json.loads(json.dumps(parsed, sort_keys=True, ensure_ascii=False))
        return canonical

    elif fmt == 'toml':
        try:
            parsed = toml.loads(raw)
        except toml.TomlDecodeError as e:
            raise ValueError(f'Invalid TOML: {e}')

        if not isinstance(parsed, dict):
            raise ValueError('Config must be a TOML table (object)')

        # Check for non-JSON-native types (datetime, etc.)
        def check_json_types(obj, path=''):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    check_json_types(v, f'{path}/{k}' if path else f'/{k}')
            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    check_json_types(item, f'{path}/{i}' if path else f'/{i}')
            elif isinstance(obj, (datetime.datetime, datetime.time, datetime.date)):
                raise ValueError('non_json_type')

        try:
            import datetime
            check_json_types(parsed)
        except ValueError as e:
            if 'non_json_type' in str(e):
                raise
            raise

        # Canonicalize
        canonical = json.loads(json.dumps(parsed, sort_keys=True, ensure_ascii=False))
        return canonical

    else:
        raise ValueError('unsupported_format')


def validate_schema_for_refs(schema: dict, parent_path: str = ''):
    """Validate that $ref only references within the same document."""
    if isinstance(schema, dict):
        if '$ref' in schema:
            ref = schema['$ref']
            # Check if it's an external reference (starts with http/https or includes /)
            if ref.startswith('http://') or ref.startswith('https://'):
                raise ValueError('external_ref_not_allowed')
            # Local refs are allowed (e.g., #/$defs/MyDef)

        for key, value in schema.items():
            validate_schema_for_refs(value, f'{parent_path}/{key}' if parent_path else key)
    elif isinstance(schema, list):
        for i, item in enumerate(schema):
            validate_schema_for_refs(item, f'{parent_path}/{i}' if parent_path else f'/{i}')


def validate_against_schema(config: dict, schema: dict) -> dict | None:
    """Validate config against schema, return first error details or None."""
    try:
        validator = Draft202012Validator(schema)
        errors = list(validator.iter_errors(config))
        if not errors:
            return None

        # Sort errors lexicographically by path, return first
        sorted_errors = sorted(errors, key=lambda e: str(e.absolute_path))
        first_error = sorted_errors[0]

        # Build error details
        path = '/' + '/'.join(str(p) for p in first_error.absolute_path) if first_error.absolute_path else '/'

        # Determine rule and expected/actual
        rule = first_error.validator
        expected = None
        actual = None

        if rule == 'type':
            expected = first_error.validator_value
            if isinstance(first_error.instance, bool):
                actual = 'boolean'
            elif isinstance(first_error.instance, dict):
                actual = 'object'
            elif isinstance(first_error.instance, list):
                actual = 'array'
            elif isinstance(first_error.instance, str):
                actual = 'string'
            elif isinstance(first_error.instance, (int, float)):
                actual = 'number'
            elif first_error.instance is None:
                actual = 'null'
            else:
                actual = type(first_error.instance).__name__
        elif rule == 'enum':
            expected = 'one of ' + str(first_error.validator_value)
        elif rule == 'required':
            expected = f"required property '{first_error.validator_value}'"
        elif rule == 'properties':
            # Additional properties not allowed
            expected = 'additional properties not allowed'
        elif rule == 'pattern':
            expected = f"pattern '{first_error.validator_value}'"
            if isinstance(first_error.instance, str):
                actual = 'string'
        elif rule == 'minLength':
            expected = f"minimum length {first_error.validator_value}"
        elif rule == 'maxLength':
            expected = f"maximum length {first_error.validator_value}"
        elif rule == 'minimum':
            expected = f"minimum value {first_error.validator_value}"
        elif rule == 'maximum':
            expected = f"maximum value {first_error.validator_value}"
        elif rule == 'multipleOf':
            expected = f"multiple of {first_error.validator_value}"
        elif rule == 'minProperties':
            expected = f"at least {first_error.validator_value} properties"
        elif rule == 'maxProperties':
            expected = f"at most {first_error.validator_value} properties"
        elif rule == 'minItems':
            expected = f"at least {first_error.validator_value} items"
        elif rule == 'maxItems':
            expected = f"at most {first_error.validator_value} items"
        elif rule == 'uniqueItems':
            expected = "items must be unique"
        elif rule == 'items':
            expected = 'array items must match schema'
        elif rule == 'allOf':
            expected = 'allOf schema validation failed'
        elif rule == 'anyOf':
            expected = 'anyOf schema validation failed'
        elif rule == 'oneOf':
            expected = 'oneOf schema validation failed'
        elif rule == 'not':
            expected = 'schema validation failed (not)'

        return {
            'path': path,
            'rule': rule,
            'expected': expected,
            'actual': actual
        }
    except Exception:
        return None


def get_effective_schema(name: str, scope: dict, schema_ref: dict = None) -> dict | None:
    """Get effective schema for a (name, scope) pair with precedence."""
    if schema_ref is not None:
        # Use provided schema_ref
        s_name = schema_ref.get('name')
        s_version = schema_ref.get('version')
        if s_name and s_version:
            key = (s_name, s_version)
            if key in schemas:
                return schemas[key]
    else:
        # Check for active binding
        scope_fs = normalize_scope(scope)
        binding_key = (name, scope_fs)
        if binding_key in schema_bindings:
            binding = schema_bindings[binding_key]
            s_name = binding.get('schema_ref', {}).get('name')
            s_version = binding.get('schema_ref', {}).get('version')
            if s_name and s_version:
                key = (s_name, s_version)
                if key in schemas:
                    return schemas[key]

    return None



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


@app.route('/v1/schemas/<schema_name>', methods=['POST'])
def create_schema(schema_name: str):
    """Create a new schema version."""
    ct = request.content_type
    if not ct:
        return error_response(
            'invalid_input',
            'Content-Type must be application/json; charset=utf-8',
            status=415
        )
    main_type = ct.split(';')[0].strip()
    if main_type != 'application/json':
        return error_response(
            'invalid_input',
            'Content-Type must be application/json; charset=utf-8',
            status=415
        )

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

    # Check body size
    if request.content_length and request.content_length > MAX_RAW_SIZE:
        return error_response(
            'too_large',
            'Request body exceeds 1 MiB limit',
            status=413
        )

    # Get schema from either structured form or raw form
    schema_dict = body.get('schema')
    raw_schema = body.get('raw_schema')
    raw_format = body.get('raw_format')

    if schema_dict is not None:
        # Structured JSON Schema form
        if not isinstance(schema_dict, dict):
            return error_response(
                'invalid_input',
                'schema must be an object',
                status=400
            )
        schema = schema_dict
    elif raw_schema is not None and raw_format is not None:
        # Raw schema form
        if not isinstance(raw_schema, str):
            return error_response(
                'invalid_input',
                'raw_schema must be a string',
                status=400
            )
        if raw_format not in ('json', 'yaml'):
            return error_response(
                'unsupported_format',
                'raw_format must be "json" or "yaml"',
                status=415
            )
        try:
            if raw_format == 'json':
                schema = json.loads(raw_schema)
            else:
                # YAML parsing with restrictions
                class SafeYAMLLoader(yaml.SafeLoader):
                    pass
                schema = yaml.load(raw_schema, Loader=SafeYAMLLoader)
        except Exception as e:
            return error_response(
                'schema_invalid',
                f'Invalid {raw_format} schema: {e}',
                status=422
            )
        if not isinstance(schema, dict):
            return error_response(
                'schema_invalid',
                'Schema must be a JSON object',
                status=422
            )
    else:
        return error_response(
            'invalid_input',
            'Either "schema" or "raw_schema" with "raw_format" is required',
            status=400
        )

    # Validate it's a valid JSON Schema 2020-12 (no external refs)
    try:
        validate_schema_for_refs(schema)
    except ValueError as e:
        if 'external_ref_not_allowed' in str(e):
            return error_response(
                'schema_invalid',
                'External $ref not allowed',
                details={'reason': 'external_ref_not_allowed'},
                status=422
            )
        raise

    # Additional basic schema validation
    if '$schema' not in schema:
        # Not strictly required but helps identify schema version
        pass

    # Get next version number for this schema name
    if schema_name not in schema_versions:
        schema_versions[schema_name] = []

    versions_list = schema_versions[schema_name]
    if len(versions_list) >= MAX_SCHEMA_VERSIONS:
        return error_response(
            'conflict',
            f'Maximum {MAX_SCHEMA_VERSIONS} schema versions exceeded',
            status=409
        )

    next_version = max(versions_list) + 1 if versions_list else 1

    # Store schema
    schema_key = (schema_name, next_version)
    schemas[schema_key] = schema
    versions_list.append(next_version)

    return success_response({
        'name': schema_name,
        'version': next_version
    }, status=201)


@app.route('/v1/schemas/<schema_name>/versions', methods=['POST'])
def list_schema_versions(schema_name: str):
    """List all versions of a schema."""
    ct = request.content_type
    if not ct:
        return error_response(
            'invalid_input',
            'Content-Type must be application/json; charset=utf-8',
            status=415
        )
    main_type = ct.split(';')[0].strip()
    if main_type != 'application/json':
        return error_response(
            'invalid_input',
            'Content-Type must be application/json; charset=utf-8',
            status=415
        )

    try:
        body = request.get_json(silent=False, force=True)
    except Exception:
        return error_response(
            'invalid_input',
            'Invalid JSON in request body',
            status=400
        )

    if schema_name not in schema_versions or not schema_versions[schema_name]:
        return error_response(
            'not_found',
            f'Schema {schema_name} not found',
            status=404
        )

    versions_list = sorted(schema_versions[schema_name])

    return success_response({
        'name': schema_name,
        'versions': versions_list
    })


@app.route('/v1/schemas/<schema_name>/<int:schema_version>', methods=['POST'])
def get_schema_version(schema_name: str, schema_version: int):
    """Get a specific schema version."""
    ct = request.content_type
    if not ct:
        return error_response(
            'invalid_input',
            'Content-Type must be application/json; charset=utf-8',
            status=415
        )
    main_type = ct.split(';')[0].strip()
    if main_type != 'application/json':
        return error_response(
            'invalid_input',
            'Content-Type must be application/json; charset=utf-8',
            status=415
        )

    try:
        body = request.get_json(silent=False, force=True)
    except Exception:
        return error_response(
            'invalid_input',
            'Invalid JSON in request body',
            status=400
        )

    schema_key = (schema_name, schema_version)
    if schema_key not in schemas:
        return error_response(
            'not_found',
            f'Schema {schema_name} version {schema_version} not found',
            status=404
        )

    return success_response({
        'name': schema_name,
        'version': schema_version,
        'schema': schemas[schema_key]
    })


@app.route('/v1/configs/<name>:bind', methods=['POST'])
def bind_schema(name: str):
    """Bind a schema to a config identity."""
    ct = request.content_type
    if not ct:
        return error_response(
            'invalid_input',
            'Content-Type must be application/json; charset=utf-8',
            status=415
        )
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
        schema_ref = body.get('schema_ref')

        if not isinstance(schema_ref, dict):
            return error_response(
                'invalid_input',
                'schema_ref must be an object',
                status=400
            )

        s_name = schema_ref.get('name')
        s_version = schema_ref.get('version')

        if not isinstance(s_name, str) or not s_name:
            return error_response(
                'invalid_input',
                'schema_ref.name must be a non-empty string',
                status=400
            )
        if not isinstance(s_version, int) or s_version < 1:
            return error_response(
                'invalid_input',
                'schema_ref.version must be a positive integer',
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

    # Check if schema exists
    schema_key = (s_name, s_version)
    if schema_key not in schemas:
        return error_response(
            'not_found',
            f'Schema {s_name} version {s_version} not found',
            status=404
        )

    scope_fs = normalize_scope(scope)
    binding_key = (name, scope_fs)

    # Establish or update binding
    schema_bindings[binding_key] = {
        'name': name,
        'scope': scope,
        'schema_ref': {'name': s_name, 'version': s_version},
        'active': True
    }

    return success_response({
        'name': name,
        'scope': scope,
        'schema_ref': {'name': s_name, 'version': s_version},
        'active': True
    })


@app.route('/v1/configs/<name>/schema', methods=['POST'])
def get_binding(name: str):
    """Read the active binding."""
    ct = request.content_type
    if not ct:
        return error_response(
            'invalid_input',
            'Content-Type must be application/json; charset=utf-8',
            status=415
        )
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
    binding_key = (name, scope_fs)

    if binding_key not in schema_bindings:
        return error_response(
            'not_found',
            f'No schema binding found for name={name}, scope={scope}',
            status=404
        )

    binding = schema_bindings[binding_key]
    return success_response({
        'name': binding['name'],
        'scope': binding['scope'],
        'schema_ref': binding['schema_ref']
    })


@app.route('/v1/configs/<name>:validate', methods=['POST'])
def validate_config(name: str):
    """Validate a config without state changes."""
    ct = request.content_type
    if not ct:
        return error_response(
            'invalid_input',
            'Content-Type must be application/json; charset=utf-8',
            status=415
        )
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
        schema_ref = body.get('schema_ref')
        if schema_ref is not None and not isinstance(schema_ref, dict):
            return error_response(
                'invalid_input',
                'schema_ref must be an object',
                status=400
            )
        mode = body.get('mode', 'resolved')
        if mode not in ('stored', 'resolved'):
            return error_response(
                'invalid_input',
                'mode must be "stored" or "resolved"',
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

    # Determine which version to validate
    target_version = version
    if target_version is None:
        # Use active version
        if key not in active_version_cache:
            return error_response(
                'not_found',
                f'No active config found for name={name}, scope={scope}',
                status=404
            )
        target_version = active_version_cache[key]

    # Find the config version
    config_version = None
    if key in configs:
        for v in configs[key]:
            if v.version == target_version:
                config_version = v
                break

    if config_version is None:
        return error_response(
            'not_found',
            f'Version {target_version} not found for name={name}, scope={scope}',
            status=404
        )

    # Get effective schema
    effective_schema = get_effective_schema(name, scope, schema_ref)
    validated_against = None

    if effective_schema is not None:
        # Determine schema_ref for response
        if schema_ref:
            validated_against = schema_ref
        else:
            # Get from binding
            binding = schema_bindings.get(key)
            if binding:
                validated_against = binding['schema_ref']

    # Get config to validate
    if mode == 'resolved':
        try:
            resolved_config, _ = resolve_config(name, scope, target_version)
            config_to_validate = resolved_config
        except ValueError as e:
            return error_response(
                'not_found',
                str(e),
                status=404
            )
    else:
        config_to_validate = config_version.config

    # Validate if schema exists
    if effective_schema is not None:
        error_details = validate_against_schema(config_to_validate, effective_schema)
        if error_details is not None:
            return error_response(
                'validation_failed',
                'Config does not conform to schema',
                details=error_details,
                status=422
            )

    return success_response({
        'name': name,
        'scope': scope,
        'version_used': target_version,
        'mode': mode,
        'valid': True,
        'validated_against': validated_against
    })


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
        raw_config = body.get('raw_config')
        raw_format = body.get('raw_format')
        config = body.get('config')

        # Either raw_config or config must be provided
        if raw_config is not None and raw_format is not None:
            # Raw config ingestion
            if not isinstance(raw_config, str):
                return error_response(
                    'invalid_input',
                    'raw_config must be a string',
                    status=400
                )
            if raw_format not in ('json', 'yaml', 'toml'):
                return error_response(
                    'unsupported_format',
                    'raw_format must be "json", "yaml", or "toml"',
                    status=415
                )
            try:
                config = parse_raw_config(raw_config, raw_format)
            except ValueError as e:
                error_msg = str(e)
                if 'yaml_feature_not_allowed' in error_msg:
                    return error_response(
                        'unprocessable',
                        'YAML feature not allowed',
                        details={'reason': 'yaml_feature_not_allowed'},
                        status=422
                    )
                elif 'non_json_type' in error_msg:
                    return error_response(
                        'unprocessable',
                        'Non-JSON-native type in TOML',
                        details={'reason': 'non_json_type'},
                        status=422
                    )
                elif error_msg == 'unsupported_format':
                    return error_response(
                        'unsupported_format',
                        'Unsupported format',
                        status=415
                    )
                else:
                    # Parse error
                    if 'Invalid JSON' in error_msg or 'Invalid YAML' in error_msg or 'Invalid TOML' in error_msg:
                        return error_response(
                            'unprocessable',
                            error_msg,
                            status=422
                        )
                    return error_response(
                        'invalid_input',
                        error_msg,
                        status=400
                    )
        elif config is not None:
            # Structured JSON config
            if not isinstance(config, dict):
                return error_response(
                    'invalid_input',
                    'config must be an object',
                    status=400
                )
        else:
            return error_response(
                'invalid_input',
                'Either "config" or "raw_config" with "raw_format" is required',
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
        schema_ref_override = body.get('schema_ref')
        if schema_ref_override is not None and not isinstance(schema_ref_override, dict):
            return error_response(
                'invalid_input',
                'schema_ref must be an object',
                status=400
            )
    except ValueError as e:
        return error_response('invalid_input', str(e), status=400)

    scope_fs = normalize_scope(scope)
    key = (name, scope_fs)

    # Determine effective schema
    effective_schema = get_effective_schema(name, scope, schema_ref_override)
    validated_against = None

    if effective_schema is not None:
        if schema_ref_override:
            validated_against = schema_ref_override
        else:
            binding = schema_bindings.get(key)
            if binding:
                validated_against = binding['schema_ref']

        # Validate config against schema
        error_details = validate_against_schema(config, effective_schema)
        if error_details is not None:
            return error_response(
                'validation_failed',
                'Config does not conform to schema',
                details=error_details,
                status=422
            )

    # Canonicalize body for idempotency check (excluding raw_config for canonicalization)
    canonical_body_dict = {
        'scope': scope,
        'config': config,
        'includes': includes
    }
    if schema_ref_override:
        canonical_body_dict['schema_ref'] = schema_ref_override
    canonical_body = json.dumps(canonical_body_dict, sort_keys=True, ensure_ascii=False)

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
        canonical_body=canonical_body,
        schema_ref=validated_against
    )

    # Deactivate all previous versions
    for v in configs[key]:
        v.active = False

    configs[key].append(new_version)
    active_version_cache[key] = next_version

    response_data = {
        'name': name,
        'scope': scope,
        'version': next_version,
        'active': True
    }
    if validated_against:
        response_data['validated_against'] = validated_against

    return success_response(response_data, status=201)


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
