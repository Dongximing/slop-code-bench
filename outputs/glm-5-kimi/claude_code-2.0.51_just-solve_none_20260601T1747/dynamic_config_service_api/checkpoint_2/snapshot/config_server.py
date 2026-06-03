#!/usr/bin/env python3
"""Configuration server with schema registry, validation, and raw config ingestion."""

import argparse
import hashlib
import io
import json
import re
import threading
from collections import OrderedDict
from copy import deepcopy
from decimal import Decimal
from typing import Any

import tomli
import yaml
from flask import Flask, Response, request
from jsonschema import Draft202012Validator, ValidationError, RefResolver
from jsonschema.exceptions import SchemaError

MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MiB
MAX_SCHEMA_VERSIONS = 1_000
MAX_CONFIG_VERSIONS = 10_000
MAX_INCLUDE_CHAIN = 64


class SchemaRegistry:
    """Thread-safe schema storage with versioning."""

    def __init__(self):
        self._lock = threading.Lock()
        # schema_name -> OrderedDict[version -> schema_dict]
        self._schemas: dict[str, OrderedDict[int, dict]] = {}

    def create(self, name: str, schema: dict) -> tuple[dict | None, dict | None, int]:
        """Create a new schema version. Returns (result, error, status)."""
        with self._lock:
            versions = self._schemas.get(name)
            next_version = 1

            if versions is None:
                versions = OrderedDict()
                self._schemas[name] = versions
            else:
                if len(versions) >= MAX_SCHEMA_VERSIONS:
                    return None, _make_error("conflict", "Max schema versions (1,000) reached"), 409
                next_version = max(versions.keys()) + 1

            versions[next_version] = deepcopy(schema)
            return {"name": name, "version": next_version}, None, 201

    def get(self, name: str, version: int) -> tuple[dict | None, int]:
        """Get a specific schema version. Returns (schema, status)."""
        with self._lock:
            versions = self._schemas.get(name)
            if versions is None or version not in versions:
                return None, 404
            return deepcopy(versions[version]), 200

    def list_versions(self, name: str) -> tuple[dict | None, int]:
        """List all versions of a schema. Returns (result, status)."""
        with self._lock:
            versions = self._schemas.get(name)
            if versions is None:
                return None, 404
            return {"name": name, "versions": sorted(versions.keys())}, 200

    def exists(self, name: str, version: int) -> bool:
        """Check if a schema version exists."""
        with self._lock:
            versions = self._schemas.get(name)
            return versions is not None and version in versions


class BindingStore:
    """Thread-safe storage for config-to-schema bindings."""

    def __init__(self):
        self._lock = threading.Lock()
        # (name, scope_key) -> {"name": ..., "version": ...}
        self._bindings: dict[tuple[str, str], dict] = {}

    def bind(self, name: str, scope: dict, schema_ref: dict) -> dict:
        """Create or update a binding. Returns the binding info."""
        with self._lock:
            key = (name, _scope_key(scope))
            self._bindings[key] = {
                "name": schema_ref["name"],
                "version": schema_ref["version"],
            }
            return {
                "name": name,
                "scope": scope,
                "schema_ref": schema_ref,
                "active": True,
            }

    def get(self, name: str, scope: dict) -> dict | None:
        """Get the binding for a config identity."""
        with self._lock:
            key = (name, _scope_key(scope))
            binding = self._bindings.get(key)
            if binding is None:
                return None
            return {
                "name": name,
                "scope": scope,
                "schema_ref": binding,
            }


class ConfigStore:

    def __init__(self):
        self._lock = threading.Lock()
        self._configs: dict[tuple[str, str], OrderedDict[int, "ConfigVersion"]] = {}
        self._active: dict[tuple[str, str], int] = {}
        self._idempotency: dict[str, tuple] = {}

    @staticmethod
    def scope_key(scope: dict[str, str]) -> str:
        return _scope_key(scope)

    def _pair_key(self, name: str, scope: dict[str, str]) -> tuple[str, str]:
        return (name, self.scope_key(scope))

    def create(
        self,
        name: str,
        scope: dict[str, str],
        config: dict,
        includes: list | None,
        inherits_active: bool,
        body_hash: str,
    ) -> tuple[dict | None, dict | None, int | None]:
        with self._lock:
            pair = self._pair_key(name, scope)

            existing = self._idempotency.get(body_hash)
            if existing is not None:
                ename, escope_key, eversion = existing
                if ename == name and escope_key == pair[1]:
                    return {
                        "name": name,
                        "scope": scope,
                        "version": eversion,
                        "active": self._active.get(pair) == eversion,
                    }, None, None

            versions = self._configs.get(pair)
            next_version = 1
            if versions is None:
                versions = OrderedDict()
                self._configs[pair] = versions
            else:
                if len(versions) >= MAX_CONFIG_VERSIONS:
                    return None, _make_error("conflict", "Max versions (10,000) reached for this config"), 409
                next_version = max(versions.keys()) + 1

            if inherits_active and pair in self._active:
                active_ver = self._active[pair]
                active_cv = versions[active_ver]
                merged_config = self._deep_merge_simple(active_cv.config, config)
                config = merged_config
                if includes is None:
                    includes = deepcopy(active_cv.includes)
                else:
                    if active_cv.includes:
                        includes = deepcopy(active_cv.includes) + includes

            if includes is None:
                includes = []

            cv = ConfigVersion(
                name=name,
                scope=scope,
                version=next_version,
                config=config,
                includes=includes,
            )
            versions[next_version] = cv
            self._active[pair] = next_version
            self._idempotency[body_hash] = (name, pair[1], next_version)

            return {
                "name": name,
                "scope": scope,
                "version": next_version,
                "active": True,
            }, None, 201

    def list_versions(self, name: str, scope: dict[str, str]) -> tuple[dict, int]:
        pair = self._pair_key(name, scope)
        with self._lock:
            versions = self._configs.get(pair)
            if versions is None:
                return {
                    "name": name,
                    "scope": scope,
                    "versions": [],
                }, 200
            active_ver = self._active.get(pair)
            result = []
            for v in sorted(versions.keys()):
                result.append({
                    "version": v,
                    "active": v == active_ver,
                })
            return {
                "name": name,
                "scope": scope,
                "versions": result,
            }, 200

    def get_version(self, name: str, scope: dict[str, str], version: int) -> tuple[dict | None, int]:
        pair = self._pair_key(name, scope)
        with self._lock:
            versions = self._configs.get(pair)
            if versions is None or version not in versions:
                return None, 404
            cv = versions[version]
            active_ver = self._active.get(pair)
            return {
                "name": name,
                "scope": scope,
                "version": version,
                "active": version == active_ver,
                "config": cv.config,
                "includes": cv.includes,
            }, 200

    def get_active(self, name: str, scope: dict[str, str]) -> tuple[dict | None, int]:
        pair = self._pair_key(name, scope)
        with self._lock:
            active_ver = self._active.get(pair)
            if active_ver is None:
                return None, 404
            versions = self._configs.get(pair)
            cv = versions[active_ver]
            return {
                "name": name,
                "scope": scope,
                "version": active_ver,
                "active": True,
                "config": cv.config,
                "includes": cv.includes,
            }, 200

    def activate(self, name: str, scope: dict[str, str], version: int) -> tuple[dict | None, int]:
        pair = self._pair_key(name, scope)
        with self._lock:
            versions = self._configs.get(pair)
            if versions is None or version not in versions:
                return None, 404
            self._active[pair] = version
            return {
                "name": name,
                "scope": scope,
                "version": version,
                "active": True,
            }, 200

    def rollback(self, name: str, scope: dict[str, str], to_version: int) -> tuple[dict | None, int]:
        pair = self._pair_key(name, scope)
        with self._lock:
            active_ver = self._active.get(pair)
            if active_ver is not None and to_version > active_ver:
                return None, 409
            versions = self._configs.get(pair)
            if versions is None or to_version not in versions:
                return None, 404
            if active_ver == to_version:
                return {
                    "name": name,
                    "scope": scope,
                    "version": to_version,
                    "active": True,
                }, 200
            self._active[pair] = to_version
            return {
                "name": name,
                "scope": scope,
                "version": to_version,
                "active": True,
            }, 200

    def resolve(
        self,
        name: str,
        scope: dict[str, str],
        version: int | None,
        dry_run: bool,
    ) -> tuple[dict | None, dict | None, int]:
        graph: list[dict] = []
        visited: set[tuple[str, str, int]] = set()

        try:
            resolved = self._resolve_recursive(
                name, scope, version, graph, visited, dry_run
            )
        except CycleError as e:
            return None, _make_error("cycle_detected", str(e)), 409
        except MergeConflictError as e:
            return None, _make_error("unprocessable", str(e), {"path": e.path}), 422
        except MaxDepthError as e:
            return None, _make_error("unprocessable", str(e), {"reason": "max_depth"}), 422
        except NotFoundError as e:
            return None, _make_error("not_found", str(e)), 404

        return {
            "name": name,
            "scope": scope,
            "version_used": self._get_version_used(name, scope, version),
            "resolved_config": resolved,
            "resolution_graph": graph,
        }, None, 200

    def _get_version_used(self, name: str, scope: dict[str, str], version: int | None) -> int:
        pair = self._pair_key(name, scope)
        with self._lock:
            if version is not None:
                return version
            return self._active.get(pair, 0)

    def _resolve_recursive(
        self,
        name: str,
        scope: dict[str, str],
        version: int | None,
        graph: list[dict],
        visited: set[tuple[str, str, int]],
        dry_run: bool,
    ) -> dict:
        pair = self._pair_key(name, scope)

        with self._lock:
            versions = self._configs.get(pair)
            if versions is None:
                if dry_run:
                    return {}
                raise NotFoundError(f"Config '{name}' with scope not found")
            if version is None:
                active_ver = self._active.get(pair)
                if active_ver is None:
                    if dry_run:
                        return {}
                    raise NotFoundError(f"No active version for '{name}' with scope")
                version = active_ver
            if version not in versions:
                raise NotFoundError(f"Version {version} not found for '{name}' with scope")
            cv = versions[version]
            includes = deepcopy(cv.includes)
            own_config = deepcopy(cv.config)

        trip = (name, self.scope_key(scope), version)
        if trip in visited:
            raise CycleError(f"Cycle detected involving '{name}' version {version}")
        if len(visited) >= MAX_INCLUDE_CHAIN:
            raise MaxDepthError("Max include chain length (64) exceeded")
        visited.add(trip)

        try:
            accumulator: dict = {}

            for inc in includes:
                inc_name = inc["name"]
                inc_scope = inc["scope"]
                inc_version = inc.get("version")
                inc_resolved = self._resolve_recursive(
                    inc_name, inc_scope, inc_version, graph, visited, dry_run
                )
                accumulator = self._deep_merge(accumulator, inc_resolved)

            result = self._deep_merge(accumulator, own_config)

            graph.append({
                "name": name,
                "scope": scope,
                "version_used": version,
            })

            return result
        finally:
            visited.discard(trip)

    @staticmethod
    def _deep_merge(base: dict, override: dict, path: str = "") -> dict:
        result = deepcopy(base)
        for key in override:
            new_path = f"{path}/{key}" if path else f"/{key}"
            if key in result:
                base_val = result[key]
                over_val = override[key]
                if isinstance(base_val, dict) and isinstance(over_val, dict):
                    result[key] = ConfigStore._deep_merge(base_val, over_val, new_path)
                elif isinstance(base_val, dict) or isinstance(over_val, dict):
                    raise MergeConflictError(
                        f"Type conflict at {new_path}: cannot merge dict with non-dict",
                        new_path,
                    )
                else:
                    result[key] = deepcopy(over_val)
            else:
                result[key] = deepcopy(override[key])
        return result

    @staticmethod
    def _deep_merge_simple(base: dict, override: dict) -> dict:
        result = deepcopy(base)
        for key in override:
            if key in result and isinstance(result[key], dict) and isinstance(override[key], dict):
                result[key] = ConfigStore._deep_merge_simple(result[key], override[key])
            else:
                result[key] = deepcopy(override[key])
        return result


class ConfigVersion:
    """Immutable config version."""

    __slots__ = ("name", "scope", "version", "config", "includes")

    def __init__(
        self,
        name: str,
        scope: dict[str, str],
        version: int,
        config: dict,
        includes: list,
    ):
        self.name = name
        self.scope = scope
        self.version = version
        self.config = config
        self.includes = includes


class CycleError(Exception):
    pass


class MergeConflictError(Exception):
    def __init__(self, message: str, path: str):
        super().__init__(message)
        self.path = path


class MaxDepthError(Exception):
    pass


class NotFoundError(Exception):
    pass


def _scope_key(scope: dict[str, str]) -> str:
    return json.dumps(scope, sort_keys=True, separators=(",", ":"))


def _make_error(code: str, message: str, details: dict | None = None) -> dict:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
        }
    }


def _canonicalize(obj: Any) -> Any:
    """Recursively canonicalize a JSON value."""
    if isinstance(obj, dict):
        return {k: _canonicalize(v) for k, v in sorted(obj.items())}
    elif isinstance(obj, list):
        return [_canonicalize(item) for item in obj]
    elif isinstance(obj, float):
        # Canonicalize floats: handle special values and precision
        if obj != obj:  # NaN
            return "NaN"
        elif obj == float('inf'):
            return "Infinity"
        elif obj == float('-inf'):
            return "-Infinity"
        else:
            return obj
    elif isinstance(obj, (int, str, bool)) or obj is None:
        return obj
    else:
        return obj


# Global stores
schema_registry = SchemaRegistry()
binding_store = BindingStore()
config_store = ConfigStore()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_BODY_BYTES


def _json_response(data: dict, status: int = 200) -> Response:
    body = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    resp = Response(body + "\n", status=status, content_type="application/json; charset=utf-8")
    return resp


def _error_response(code: str, message: str, status: int, details: dict | None = None) -> Response:
    return _json_response(
        {"error": {"code": code, "message": message, "details": details or {}}},
        status,
    )


def _parse_body():
    if request.content_length and request.content_length > MAX_BODY_BYTES:
        return None, _error_response("too_large", "Request body exceeds 1 MiB limit", 413)

    try:
        data = request.get_json(force=True, silent=True)
    except Exception:
        return None, _error_response("invalid_input", "Invalid JSON in request body", 400)

    if data is None:
        return None, _error_response("invalid_input", "Invalid JSON in request body", 400)

    return data, None


def _validate_scope(data: dict, field: str = "scope") -> tuple[dict | None, Response | None]:
    if field not in data:
        return None, _error_response("invalid_input", f"Missing required field: {field}", 400)

    scope = data[field]
    if not isinstance(scope, dict):
        return None, _error_response("invalid_input", f"'{field}' must be a JSON object", 400)

    for k, v in scope.items():
        if not isinstance(k, str) or not isinstance(v, str):
            return None, _error_response(
                "invalid_input", f"'{field}' must be a string-to-string map", 400
            )

    return scope, None


def _validate_includes(includes: list) -> tuple[bool, Response | None]:
    for i, inc in enumerate(includes):
        if not isinstance(inc, dict):
            return False, _error_response(
                "invalid_input", f"Include at index {i} must be an object", 400
            )
        if "name" not in inc or not isinstance(inc["name"], str) or not inc["name"]:
            return False, _error_response(
                "invalid_input", f"Include at index {i} must have a non-empty 'name' string", 400
            )
        if "scope" not in inc or not isinstance(inc["scope"], dict):
            return False, _error_response(
                "invalid_input", f"Include at index {i} must have a 'scope' object", 400
            )
        for k, v in inc["scope"].items():
            if not isinstance(k, str) or not isinstance(v, str):
                return False, _error_response(
                    "invalid_input", f"Include at index {i} scope must be string-to-string map", 400
                )
        if "version" in inc and inc["version"] is not None:
            if not isinstance(inc["version"], int) or inc["version"] < 1:
                return False, _error_response(
                    "invalid_input", f"Include at index {i} version must be a positive integer or null", 400
                )
    return True, None


def _validate_schema_ref(schema_ref: dict) -> tuple[bool, Response | None]:
    """Validate a schema_ref structure."""
    if not isinstance(schema_ref, dict):
        return False, _error_response("invalid_input", "'schema_ref' must be an object", 400)
    if "name" not in schema_ref or not isinstance(schema_ref["name"], str):
        return False, _error_response("invalid_input", "'schema_ref.name' must be a string", 400)
    if "version" not in schema_ref or not isinstance(schema_ref["version"], int):
        return False, _error_response("invalid_input", "'schema_ref.version' must be an integer", 400)
    return True, None


# Custom YAML handling to detect disallowed features
class YamlSafeLoader(yaml.SafeLoader):
    pass


def _yaml_construct_undefined(loader, node):
    raise yaml.constructor.ConstructorError(
        None, None, "custom tags are not allowed", node.start_mark
    )


def _yaml_construct_anchor(loader, node):
    # This handles anchors - we need to detect them before they're processed
    pass


# Override default constructors for disallowed features
YamlSafeLoader.add_constructor(None, _yaml_construct_undefined)
YamlSafeLoader.yaml_constructors = yaml.SafeLoader.yaml_constructors.copy()


def _parse_yaml(raw: str) -> tuple[Any | None, dict | None]:
    """Parse YAML string, checking for disallowed features."""
    # Check for merge key
    if '<<:' in raw or '<< :' in raw or '<<\n' in raw or '<< ' in raw:
        return None, {"reason": "yaml_feature_not_allowed"}

    # Check for anchors and aliases
    if re.search(r'&\w+', raw) or re.search(r'\*\w+', raw):
        return None, {"reason": "yaml_feature_not_allowed"}

    # Check for custom tags (including merge key)
    if re.search(r'!!\w+', raw) or re.search(r'!\w+', raw):
        # Allow only standard YAML tags
        allowed_tags = {'!!str', '!!int', '!!float', '!!bool', '!!null',
                        '!!seq', '!!map', '!!set', '!!omap', '!!pairs',
                        '!!binary', '!!timestamp', '!!merge'}
        for match in re.finditer(r'!!?\w+', raw):
            tag = match.group()
            if tag == '<<':
                return None, {"reason": "yaml_feature_not_allowed"}
            if tag.startswith('!!') and tag not in allowed_tags:
                return None, {"reason": "yaml_feature_not_allowed"}
            if tag.startswith('!') and not tag.startswith('!!'):
                return None, {"reason": "yaml_feature_not_allowed"}

    try:
        # Use safe load with custom checks
        loader = yaml.SafeLoader(raw)
        data = loader.get_single_data()

        # Check that all keys are strings
        def check_string_keys(obj):
            if isinstance(obj, dict):
                for k in obj.keys():
                    if not isinstance(k, str):
                        return False
                    if not check_string_keys(obj[k]):
                        return False
            elif isinstance(obj, list):
                for item in obj:
                    if not check_string_keys(item):
                        return False
            return True

        if not check_string_keys(data):
            return None, {"reason": "yaml_feature_not_allowed"}

        return data, None
    except yaml.YAMLError:
        return None, {"reason": "parse_error"}


def _parse_toml(raw: str) -> tuple[Any | None, dict | None]:
    """Parse TOML string, rejecting non-JSON-native types."""
    try:
        data = tomli.loads(raw)

        # Check for non-JSON-native types
        def check_json_types(obj):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if not check_json_types(v):
                        return False
                return True
            elif isinstance(obj, list):
                for item in obj:
                    if not check_json_types(item):
                        return False
                return True
            elif isinstance(obj, (str, int, float, bool)) or obj is None:
                return True
            else:
                # Reject datetime, date, time, and other non-JSON types
                return False

        if not check_json_types(data):
            return None, {"reason": "non_json_type"}

        return data, None
    except tomli.TOMLDecodeError:
        return None, {"reason": "parse_error"}


def _parse_json_strict(raw: str) -> tuple[Any | None, dict | None]:
    """Parse JSON string strictly (no comments, trailing commas)."""
    try:
        # Standard json.loads is strict by default (no comments, no trailing commas)
        data = json.loads(raw)
        return data, None
    except json.JSONDecodeError:
        return None, {"reason": "parse_error"}


def _parse_raw_config(raw: str, fmt: str) -> tuple[Any | None, dict | None, int | None]:
    """Parse raw config string in specified format.
    Returns (parsed_data, error_details, http_status)."""
    if fmt == "json":
        data, err = _parse_json_strict(raw)
        if err:
            return None, err, 422
    elif fmt == "yaml":
        data, err = _parse_yaml(raw)
        if err:
            return None, err, 422
    elif fmt == "toml":
        data, err = _parse_toml(raw)
        if err:
            return None, err, 422
    else:
        return None, None, 415  # unsupported_format

    # Ensure root is an object
    if not isinstance(data, dict):
        return None, {"reason": "root_not_object"}, 422

    return _canonicalize(data), None, None


def _parse_raw_schema(raw: str, fmt: str) -> tuple[dict | None, dict | None, int | None]:
    """Parse raw schema string. Returns (parsed_schema, error_details, http_status)."""
    if fmt not in ("json", "yaml"):
        return None, None, 415

    if fmt == "json":
        data, err = _parse_json_strict(raw)
        if err:
            return None, err, 422
    else:  # yaml
        data, err = _parse_yaml(raw)
        if err:
            return None, err, 422

    if not isinstance(data, dict):
        return None, {"reason": "schema_not_object"}, 422

    return data, None, None


def _check_external_refs(schema: dict) -> bool:
    """Check if schema contains external $ref (HTTP/remote)."""
    def check(obj):
        if isinstance(obj, dict):
            if "$ref" in obj:
                ref = obj["$ref"]
                if isinstance(ref, str):
                    # External refs start with http://, https://, or are not # (fragment-only)
                    if ref.startswith("http://") or ref.startswith("https://"):
                        return False
                    # Check for other URI schemes (not just fragments)
                    if not ref.startswith("#") and "://" in ref:
                        return False
            for v in obj.values():
                if not check(v):
                    return False
        elif isinstance(obj, list):
            for item in obj:
                if not check(item):
                    return False
        return True

    return check(schema)


def _validate_schema_structure(schema: dict) -> tuple[bool, dict | None]:
    """Validate that the schema is a valid JSON Schema Draft 2020-12."""
    try:
        # Check for external refs first
        if not _check_external_refs(schema):
            return False, {"reason": "external_ref_not_allowed"}

        # Create a validator that doesn't resolve remote refs
        # We use a custom resolver that only allows in-document refs
        base_uri = "urn:schema:"
        resolver = RefResolver(base_uri, schema)

        # Try to create a validator - this validates the schema structure
        validator = Draft202012Validator(schema, resolver=resolver)

        # Test the schema by checking if it's valid
        # This will raise SchemaError if the schema is invalid
        validator.check_schema(schema)

        return True, None
    except SchemaError as e:
        return False, {"reason": "invalid_schema", "message": str(e)}
    except Exception as e:
        return False, {"reason": "invalid_schema", "message": str(e)}


def _get_validation_error_details(error: ValidationError) -> dict:
    """Extract validation error details from a jsonschema ValidationError."""
    # Build the JSON Pointer path
    path = "/" + "/".join(str(p) for p in error.absolute_path) if error.absolute_path else "/"

    # Determine the rule that failed
    rule = error.validator or "unknown"

    details = {
        "path": path,
        "rule": rule,
    }

    # Add expected/actual based on the validation type
    if error.validator == "type":
        details["expected"] = error.validator_value
        details["actual"] = type(error.instance).__name__
    elif error.validator == "enum":
        details["expected"] = error.validator_value
        details["actual"] = error.instance
    elif error.validator == "const":
        details["expected"] = error.validator_value
        details["actual"] = error.instance
    elif error.validator == "pattern":
        details["expected"] = error.validator_value
        details["actual"] = error.instance
    elif error.validator == "minimum":
        details["expected"] = f">= {error.validator_value}"
        details["actual"] = error.instance
    elif error.validator == "maximum":
        details["expected"] = f"<= {error.validator_value}"
        details["actual"] = error.instance
    elif error.validator == "minLength":
        details["expected"] = f">= {error.validator_value} characters"
        details["actual"] = f"{len(error.instance)} characters"
    elif error.validator == "maxLength":
        details["expected"] = f"<= {error.validator_value} characters"
        details["actual"] = f"{len(error.instance)} characters"
    elif error.validator == "required":
        details["expected"] = error.validator_value
    elif error.validator == "additionalProperties":
        details["actual"] = list(error.instance.keys())

    return details


def _validate_against_schema(config: dict, schema: dict) -> tuple[bool, dict | None]:
    """Validate config against a JSON Schema. Returns (valid, error_details)."""
    try:
        base_uri = "urn:schema:"
        resolver = RefResolver(base_uri, schema)
        validator = Draft202012Validator(schema, resolver=resolver)

        errors = list(validator.iter_errors(config))
        if not errors:
            return True, None

        # Find the lexicographically smallest path among errors
        def get_error_sort_key(err):
            path = "/" + "/".join(str(p) for p in err.absolute_path) if err.absolute_path else "/"
            return path

        sorted_errors = sorted(errors, key=get_error_sort_key)
        first_error = sorted_errors[0]

        return False, _get_validation_error_details(first_error)
    except Exception as e:
        return False, {"reason": "validation_error", "message": str(e)}


def _get_effective_schema(
    name: str,
    scope: dict,
    schema_ref: dict | None,
) -> tuple[dict | None, dict | None, int | None]:
    """Get the effective schema for validation.
    Returns (schema, schema_info, error_response, status).
    schema_info is {"name": ..., "version": ...} if schema exists.
    """
    effective_ref = schema_ref

    if effective_ref is None:
        # Try to get from binding
        binding = binding_store.get(name, scope)
        if binding is not None:
            effective_ref = binding["schema_ref"]

    if effective_ref is None:
        return None, None, None, None

    # Check if schema exists
    if not schema_registry.exists(effective_ref["name"], effective_ref["version"]):
        return None, None, _make_error("not_found", f"Schema '{effective_ref['name']}' version {effective_ref['version']} not found"), 404

    schema, status = schema_registry.get(effective_ref["name"], effective_ref["version"])
    if schema is None:
        return None, None, _make_error("not_found", f"Schema '{effective_ref['name']}' version {effective_ref['version']} not found"), 404

    return schema, effective_ref, None, None


# Health check endpoint
@app.route("/healthz", methods=["GET"])
def healthz():
    return _json_response({"ok": True}, 200)


# Schema endpoints
@app.route("/v1/schemas/<schema_name>", methods=["POST"])
def create_schema(schema_name: str):
    if not schema_name:
        return _error_response("invalid_input", "Schema name cannot be empty", 400)

    data, err = _parse_body()
    if err:
        return err

    schema = None

    # Check for structured or raw schema
    if "schema" in data:
        schema = data["schema"]
        if not isinstance(schema, dict):
            return _error_response("invalid_input", "'schema' must be an object", 400)
    elif "raw_schema" in data:
        raw_schema = data["raw_schema"]
        raw_format = data.get("raw_format", "json")

        if not isinstance(raw_schema, str):
            return _error_response("invalid_input", "'raw_schema' must be a string", 400)

        if raw_format not in ("json", "yaml"):
            return _error_response("unsupported_format", f"Unsupported format: {raw_format}", 415)

        # Check size limit (1 MiB)
        if len(raw_schema.encode('utf-8')) > MAX_BODY_BYTES:
            return _error_response("too_large", "Raw schema exceeds 1 MiB limit", 413)

        parsed, err_details, status = _parse_raw_schema(raw_schema, raw_format)
        if err_details or status:
            if status == 415:
                return _error_response("unsupported_format", f"Unsupported format: {raw_format}", 415)
            return _error_response("schema_invalid", "Invalid schema", 422, err_details)

        schema = parsed
    else:
        return _error_response("invalid_input", "Missing 'schema' or 'raw_schema' field", 400)

    # Validate schema structure
    valid, err_details = _validate_schema_structure(schema)
    if not valid:
        return _error_response("schema_invalid", "Invalid JSON Schema", 422, err_details)

    # Store the schema (store original, not canonicalized - spec says store as provided)
    result, error, status = schema_registry.create(schema_name, schema)
    if error:
        return _json_response(error, status)

    return _json_response(result, status)


@app.route("/v1/schemas/<schema_name>/versions", methods=["POST"])
def list_schema_versions(schema_name: str):
    data, err = _parse_body()
    if err:
        return err

    result, status = schema_registry.list_versions(schema_name)
    if result is None:
        return _error_response("not_found", f"Schema '{schema_name}' not found", 404)

    return _json_response(result, status)


@app.route("/v1/schemas/<schema_name>/<int:version>", methods=["POST"])
def get_schema_version(schema_name: str, version: int):
    data, err = _parse_body()
    if err:
        return err

    result, status = schema_registry.get(schema_name, version)
    if result is None:
        return _error_response("not_found", f"Schema '{schema_name}' version {version} not found", 404)

    return _json_response({"name": schema_name, "version": version, "schema": result}, status)


# Binding endpoints
@app.route("/v1/configs/<name>:bind", methods=["POST"])
def bind_schema(name: str):
    if not name:
        return _error_response("invalid_input", "Config name cannot be empty", 400)

    data, err = _parse_body()
    if err:
        return err

    scope, err = _validate_scope(data)
    if err:
        return err

    if "schema_ref" not in data:
        return _error_response("invalid_input", "Missing required field: schema_ref", 400)

    schema_ref = data["schema_ref"]
    ok, err = _validate_schema_ref(schema_ref)
    if not ok:
        return err

    # Check if schema exists
    if not schema_registry.exists(schema_ref["name"], schema_ref["version"]):
        return _error_response("not_found", f"Schema '{schema_ref['name']}' version {schema_ref['version']} not found", 404)

    result = binding_store.bind(name, scope, schema_ref)
    return _json_response(result, 200)


@app.route("/v1/configs/<name>/schema", methods=["POST"])
def get_binding(name: str):
    if not name:
        return _error_response("invalid_input", "Config name cannot be empty", 400)

    data, err = _parse_body()
    if err:
        return err

    scope, err = _validate_scope(data)
    if err:
        return err

    result = binding_store.get(name, scope)
    if result is None:
        return _error_response("schema_not_bound", f"No schema bound to config '{name}' with given scope", 404)

    return _json_response(result, 200)


# Config endpoints (extended)
@app.route("/v1/configs/<name>", methods=["POST"])
def create_config(name: str):
    if not name:
        return _error_response("invalid_input", "Config name cannot be empty", 400)

    data, err = _parse_body()
    if err:
        return err

    scope, err = _validate_scope(data)
    if err:
        return err

    # Determine if we have structured config or raw config
    config = None
    if "config" in data:
        config = data["config"]
        if not isinstance(config, dict):
            return _error_response("invalid_input", "'config' must be a JSON object", 400)
    elif "raw_config" in data:
        raw_config = data["raw_config"]
        raw_format = data.get("raw_format", "json")

        if not isinstance(raw_config, str):
            return _error_response("invalid_input", "'raw_config' must be a string", 400)

        if raw_format not in ("json", "yaml", "toml"):
            return _error_response("unsupported_format", f"Unsupported format: {raw_format}", 415)

        # Check size limit
        if len(raw_config.encode('utf-8')) > MAX_BODY_BYTES:
            return _error_response("too_large", "Raw config exceeds 1 MiB limit", 413)

        parsed, err_details, status = _parse_raw_config(raw_config, raw_format)
        if err_details or status:
            if status == 415:
                return _error_response("unsupported_format", f"Unsupported format: {raw_format}", 415)
            if err_details and err_details.get("reason") == "root_not_object":
                return _error_response("unprocessable", "Parsed config must be a JSON object", 422)
            return _error_response("unprocessable", "Failed to parse config", 422, err_details)

        config = parsed
    else:
        return _error_response("invalid_input", "Missing required field: config or raw_config", 400)

    # Canonicalize the config
    config = _canonicalize(config)

    # Validate includes
    includes = data.get("includes")
    if includes is not None:
        if not isinstance(includes, list):
            return _error_response("invalid_input", "'includes' must be a list", 400)
        ok, err = _validate_includes(includes)
        if not ok:
            return err

    inherits_active = data.get("inherits_active", False)
    if not isinstance(inherits_active, bool):
        return _error_response("invalid_input", "'inherits_active' must be a boolean", 400)

    # Handle schema validation
    schema_ref = data.get("schema_ref")
    if schema_ref is not None:
        ok, err = _validate_schema_ref(schema_ref)
        if not ok:
            return err

    # Get effective schema
    schema, effective_ref, error, status = _get_effective_schema(name, scope, schema_ref)
    if error:
        return _json_response(error, status)

    # Validate against schema if present
    if schema is not None:
        valid, err_details = _validate_against_schema(config, schema)
        if not valid:
            return _error_response(
                "validation_failed",
                "Config does not conform to schema",
                422,
                err_details
            )

    body_hash = hashlib.sha256(request.get_data()).hexdigest()

    result, error, status = config_store.create(name, scope, config, includes, inherits_active, body_hash)

    if error:
        return _json_response(error, status)

    if status is None:
        return _json_response(result, 201)

    return _json_response(result, status)


@app.route("/v1/configs/<name>:versions", methods=["POST"])
def list_versions(name: str):
    data, err = _parse_body()
    if err:
        return err

    scope, err = _validate_scope(data)
    if err:
        return err

    result, status = config_store.list_versions(name, scope)
    return _json_response(result, status)


@app.route("/v1/configs/<name>/<int:version>", methods=["POST"])
def get_version(name: str, version: int):
    data, err = _parse_body()
    if err:
        return err

    scope, err = _validate_scope(data)
    if err:
        return err

    result, status = config_store.get_version(name, scope, version)
    if result is None:
        return _error_response("not_found", f"Version {version} not found for config '{name}'", 404)
    return _json_response(result, status)


@app.route("/v1/configs/<name>:active", methods=["POST"])
def get_active(name: str):
    data, err = _parse_body()
    if err:
        return err

    scope, err = _validate_scope(data)
    if err:
        return err

    result, status = config_store.get_active(name, scope)
    if result is None:
        return _error_response("not_found", f"No active version for config '{name}'", 404)
    return _json_response(result, status)


@app.route("/v1/configs/<name>/<int:version>:activate", methods=["POST"])
def activate_version(name: str, version: int):
    data, err = _parse_body()
    if err:
        return err

    scope, err = _validate_scope(data)
    if err:
        return err

    result, status = config_store.activate(name, scope, version)
    if result is None:
        if status == 409:
            return _error_response("conflict", f"Cannot activate version {version}", 409)
        return _error_response("not_found", f"Version {version} not found for config '{name}'", 404)
    return _json_response(result, status)


@app.route("/v1/configs/<name>:rollback", methods=["POST"])
def rollback(name: str):
    data, err = _parse_body()
    if err:
        return err

    scope, err = _validate_scope(data)
    if err:
        return err

    to_version = data.get("to_version")
    if to_version is None:
        return _error_response("invalid_input", "Missing required field: to_version", 400)
    if not isinstance(to_version, int) or to_version < 1:
        return _error_response("invalid_input", "'to_version' must be a positive integer", 400)

    result, status = config_store.rollback(name, scope, to_version)
    if result is None:
        if status == 409:
            return _error_response(
                "conflict",
                f"Cannot rollback to version {to_version}: must be <= current active version",
                409,
            )
        return _error_response("not_found", f"Version {to_version} not found for config '{name}'", 404)
    return _json_response(result, status)


@app.route("/v1/configs/<name>:resolve", methods=["POST"])
def resolve(name: str):
    data, err = _parse_body()
    if err:
        return err

    scope, err = _validate_scope(data)
    if err:
        return err

    version = data.get("version")
    if version is not None and (not isinstance(version, int) or version < 1):
        return _error_response("invalid_input", "'version' must be a positive integer or null", 400)

    dry_run = data.get("dry_run", False)
    if not isinstance(dry_run, bool):
        return _error_response("invalid_input", "'dry_run' must be a boolean", 400)

    # Handle schema validation
    schema_ref = data.get("schema_ref")
    if schema_ref is not None:
        ok, err = _validate_schema_ref(schema_ref)
        if not ok:
            return err

    result, error, status = config_store.resolve(name, scope, version, dry_run)
    if error:
        return _json_response(error, status)

    # Get effective schema for resolved config
    schema, effective_ref, error, status = _get_effective_schema(name, scope, schema_ref)
    if error:
        return _json_response(error, status)

    # Validate resolved config against schema if present
    if schema is not None and result.get("resolved_config"):
        valid, err_details = _validate_against_schema(result["resolved_config"], schema)
        if not valid:
            return _error_response(
                "validation_failed",
                "Config does not conform to schema",
                422,
                err_details
            )
        result["validated_against"] = effective_ref

    return _json_response(result, status)


@app.route("/v1/configs/<name>:validate", methods=["POST"])
def validate_config(name: str):
    if not name:
        return _error_response("invalid_input", "Config name cannot be empty", 400)

    data, err = _parse_body()
    if err:
        return err

    scope, err = _validate_scope(data)
    if err:
        return err

    version = data.get("version")
    if version is not None and (not isinstance(version, int) or version < 1):
        return _error_response("invalid_input", "'version' must be a positive integer or null", 400)

    mode = data.get("mode", "resolved")
    if mode not in ("stored", "resolved"):
        return _error_response("invalid_input", "'mode' must be 'stored' or 'resolved'", 400)

    # Handle schema validation
    schema_ref = data.get("schema_ref")
    if schema_ref is not None:
        ok, err = _validate_schema_ref(schema_ref)
        if not ok:
            return err

    # Get the config to validate
    if version is not None:
        config_data, status = config_store.get_version(name, scope, version)
    else:
        config_data, status = config_store.get_active(name, scope)

    if config_data is None:
        return _error_response("not_found", f"Config '{name}' not found", 404)

    version_used = config_data["version"]
    stored_config = config_data["config"]

    # Get effective schema
    schema, effective_ref, error, status = _get_effective_schema(name, scope, schema_ref)
    if error:
        return _json_response(error, status)

    if schema is None:
        return _error_response("schema_not_bound", f"No schema bound to config '{name}' with given scope", 404)

    # Determine what to validate
    if mode == "stored":
        config_to_validate = stored_config
    else:  # resolved
        # Resolve the config
        result, error, status = config_store.resolve(name, scope, version, False)
        if error:
            return _json_response(error, status)
        config_to_validate = result["resolved_config"]

    # Validate
    valid, err_details = _validate_against_schema(config_to_validate, schema)

    if not valid:
        return _error_response(
            "validation_failed",
            "Config does not conform to schema",
            422,
            err_details
        )

    response = {
        "name": name,
        "scope": scope,
        "version_used": version_used,
        "mode": mode,
        "valid": True,
        "validated_against": effective_ref,
    }

    return _json_response(response, 200)


@app.errorhandler(413)
def request_entity_too_large(e):
    return _error_response("too_large", "Request body exceeds 1 MiB limit", 413)


@app.errorhandler(404)
def not_found(e):
    return _error_response("not_found", "Endpoint not found", 404)


@app.errorhandler(405)
def method_not_allowed(e):
    return _error_response("invalid_input", "Method not allowed", 405)


@app.errorhandler(500)
def internal_error(e):
    return _error_response("internal", "Internal server error", 500)


def main():
    parser = argparse.ArgumentParser(description="Configuration server")
    parser.add_argument("--address", default="0.0.0.0", help="Bind address")
    parser.add_argument("--port", type=int, default=8080, help="Bind port")
    args = parser.parse_args()

    app.run(host=args.address, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
