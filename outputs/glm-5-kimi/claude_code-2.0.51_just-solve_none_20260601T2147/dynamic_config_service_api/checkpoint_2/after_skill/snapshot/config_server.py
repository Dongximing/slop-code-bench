#!/usr/bin/env python3
"""
Config Server - A configuration management service with schema registry support.
"""

import argparse
import json
import re
import sys
from copy import deepcopy
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs
import jsonschema
from jsonschema import validate
import yaml
import toml


def normalize_value(value: Any) -> Any:
    """Normalize a JSON value to canonical form with sorted keys."""
    if isinstance(value, dict):
        return {k: normalize_value(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [normalize_value(item) for item in value]
    if isinstance(value, float) and value == int(value) and abs(value) < 10**15:
        return float(int(value))
    return value


class ParseError(Exception):
    """Error parsing raw config."""
    def __init__(self, message: str, reason: str = None):
        super().__init__(message)
        self.reason = reason


def parse_json(raw: str) -> Any:
    """Parse a JSON string with strict rules (no comments, no trailing commas)."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ParseError(f"Invalid JSON: {e}")


class SafeYamlLoader(yaml.SafeLoader):
    """Custom YAML loader that disallows anchors, aliases, custom tags, and merge keys."""
    pass


def yaml_construct_undefined(loader, node):
    raise ParseError("YAML custom tags are not allowed", "yaml_feature_not_allowed")


def yaml_construct_merge(loader, node):
    raise ParseError("YAML merge keys are not allowed", "yaml_feature_not_allowed")


SafeYamlLoader.add_constructor(None, yaml_construct_undefined)
SafeYamlLoader.add_constructor('tag:yaml.org,2002:null', lambda l, n: None)
SafeYamlLoader.add_constructor('tag:yaml.org,2002:bool', lambda l, n: l.construct_yaml_bool(n))
SafeYamlLoader.add_constructor('tag:yaml.org,2002:int', lambda l, n: l.construct_yaml_int(n))
SafeYamlLoader.add_constructor('tag:yaml.org,2002:float', lambda l, n: l.construct_yaml_float(n))
SafeYamlLoader.add_constructor('tag:yaml.org,2002:str', lambda l, n: l.construct_yaml_str(n))
SafeYamlLoader.add_constructor('tag:yaml.org,2002:seq', lambda l, n: l.construct_yaml_seq(n))
SafeYamlLoader.add_constructor('tag:yaml.org,2002:map', lambda l, n: l.construct_yaml_map(n))
SafeYamlLoader.add_constructor('tag:yaml.org,2002:binary', lambda l, n: l.construct_yaml_binary(n))
SafeYamlLoader.add_constructor('tag:yaml.org,2002:timestamp', lambda l, n: l.construct_yaml_timestamp(n))
SafeYamlLoader.add_constructor('tag:yaml.org,2002:omap', lambda l, n: l.construct_yaml_omap(n))
SafeYamlLoader.add_constructor('tag:yaml.org,2002:pairs', lambda l, n: l.construct_yaml_pairs(n))
SafeYamlLoader.add_constructor('tag:yaml.org,2002:set', lambda l, n: l.construct_yaml_set(n))


def parse_yaml(raw: str) -> Any:
    """Parse a YAML string with strict rules."""
    try:
        if '<<:' in raw or '<< :' in raw:
            raise ParseError("YAML merge keys are not allowed", "yaml_feature_not_allowed")

        if '&' in raw or '*' in raw:
            lines = raw.split('\n')
            for line in lines:
                stripped = re.sub(r'"[^"]*"', '', line)
                stripped = re.sub(r"'[^']*'", '', stripped)
                if re.search(r'&\w+', stripped):
                    raise ParseError("YAML anchors are not allowed", "yaml_feature_not_allowed")
                if re.search(r'\*\w+', stripped):
                    raise ParseError("YAML aliases are not allowed", "yaml_feature_not_allowed")

        if re.search(r'!\w+', raw):
            lines = raw.split('\n')
            for line in lines:
                stripped = re.sub(r'"[^"]*"', '', line)
                stripped = re.sub(r"'[^']*'", '', stripped)
                if re.search(r'(?<!!)!\w+', stripped):
                    raise ParseError("YAML custom tags are not allowed", "yaml_feature_not_allowed")

        result = yaml.load(raw, Loader=SafeYamlLoader)

        def check_keys(obj):
            if isinstance(obj, dict):
                for k in obj.keys():
                    if not isinstance(k, str):
                        raise ParseError("YAML mapping keys must be strings", "yaml_feature_not_allowed")
                    check_keys(obj[k])
            elif isinstance(obj, list):
                for item in obj:
                    check_keys(item)

        check_keys(result)
        return result
    except yaml.YAMLError as e:
        raise ParseError(f"Invalid YAML: {e}")


def parse_toml(raw: str) -> Any:
    """Parse a TOML string with strict rules (only JSON-representable values)."""
    try:
        result = toml.loads(raw)

        def check_json_types(obj, path=""):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    check_json_types(v, f"{path}/{k}")
            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    check_json_types(item, f"{path}/{i}")
            elif hasattr(obj, 'isoformat'):
                raise ParseError(f"Non-JSON type at {path}: datetime not allowed", "non_json_type")

        check_json_types(result)
        return result
    except toml.TomlDecodeError as e:
        raise ParseError(f"Invalid TOML: {e}")


def parse_raw_config(raw: str, fmt: str) -> Any:
    fmt = fmt.lower()
    parsers = {'json': parse_json, 'yaml': parse_yaml, 'toml': parse_toml}
    if fmt not in parsers:
        raise ParseError(f"Unsupported format: {fmt}")
    return parsers[fmt](raw)


class SchemaValidationError(Exception):
    """Error during schema validation."""
    def __init__(self, path: str, rule: str, expected: str, actual: str):
        self.path = path
        self.rule = rule
        self.expected = expected
        self.actual = actual
        super().__init__(f"Validation failed at {path}")


def get_json_type(value: Any) -> str:
    """Get the JSON type of a value."""
    if value is None:
        return "null"
    elif isinstance(value, bool):
        return "boolean"
    elif isinstance(value, int):
        return "integer"
    elif isinstance(value, float):
        return "number"
    elif isinstance(value, str):
        return "string"
    elif isinstance(value, list):
        return "array"
    elif isinstance(value, dict):
        return "object"
    return "unknown"


class ExternalRefError(Exception):
    pass


def check_external_refs(obj):
    """Raise ExternalRefError if obj contains any external $ref."""
    if isinstance(obj, dict):
        if '$ref' in obj:
            ref = obj['$ref']
            if ref.startswith(('http://', 'https://', '//')):
                raise ExternalRefError()
            if not ref.startswith('#') and ':' in ref.split('/')[0]:
                raise ExternalRefError()
        for v in obj.values():
            check_external_refs(v)
    elif isinstance(obj, list):
        for item in obj:
            check_external_refs(item)


def validate_config_against_schema(config: Any, schema: Dict) -> None:
    """Validate a config against a JSON Schema, raising SchemaValidationError on failure."""
    try:
        check_external_refs(schema)
        validate(instance=config, schema=schema)
    except jsonschema.exceptions.ValidationError as e:
        # Extract path as JSON Pointer
        path = "/" + "/".join(str(p) for p in e.absolute_path) if e.absolute_path else "/"
        rule = e.validator
        expected = ""
        actual = get_json_type(e.instance)

        if rule == "type":
            expected = e.validator_value
            if isinstance(expected, list):
                expected = ", ".join(expected)
        elif rule == "enum":
            expected = ", ".join(repr(v) for v in e.validator_value)
        elif rule == "required":
            path = path + "/" + e.validator_value[0] if e.validator_value else path
            expected = "property required"
            actual = "missing"
        elif rule == "pattern":
            expected = f"matching pattern {e.validator_value}"
        elif rule == "minimum":
            expected = f">= {e.validator_value}"
            actual = str(e.instance)
        elif rule == "maximum":
            expected = f"<= {e.validator_value}"
            actual = str(e.instance)
        elif rule == "minLength":
            expected = f"length >= {e.validator_value}"
            actual = f"length {len(e.instance)}"
        elif rule == "maxLength":
            expected = f"length <= {e.validator_value}"
            actual = f"length {len(e.instance)}"
        elif rule == "minItems":
            expected = f"items >= {e.validator_value}"
            actual = f"items {len(e.instance)}"
        elif rule == "maxItems":
            expected = f"items <= {e.validator_value}"
            actual = f"items {len(e.instance)}"
        else:
            expected = str(e.validator_value)
            actual = str(e.instance)[:50]

        raise SchemaValidationError(path, rule, expected, actual)
    except ExternalRefError:
        raise SchemaValidationError("/", "schema", "no external $ref", "external $ref found")
    except Exception as e:
        raise SchemaValidationError("/", "schema", "valid schema", str(e))


class SchemaRegistry:
    """Registry for JSON schemas with versioning."""

    def __init__(self):
        self.schemas: Dict[str, Dict[int, Dict]] = {}

    def create(self, name: str, schema: Dict) -> int:
        """Create a new schema version, returning the version number."""
        if name not in self.schemas:
            self.schemas[name] = {}

        versions = self.schemas[name]
        if len(versions) >= 1000:
            raise ValueError("Maximum schema versions exceeded")

        new_version = max(versions.keys(), default=0) + 1
        versions[new_version] = deepcopy(schema)
        return new_version

    def get(self, name: str, version: int) -> Optional[Dict]:
        """Get a specific schema version."""
        if name not in self.schemas:
            return None
        return self.schemas[name].get(version)

    def list_versions(self, name: str) -> List[int]:
        """List all versions of a schema."""
        if name not in self.schemas:
            return []
        return sorted(self.schemas[name].keys())

    def exists(self, name: str, version: int) -> bool:
        """Check if a schema version exists."""
        return name in self.schemas and version in self.schemas[name]


class ConfigStore:
    """Store for configurations with versioning."""

    def __init__(self):
        self.configs: Dict[Tuple[str, str], Dict[int, Dict]] = {}
        self.bindings: Dict[Tuple[str, str], Dict] = {}

    @staticmethod
    def scope_to_key(scope: Dict) -> str:
        """Convert a scope dict to a canonical string key."""
        return json.dumps(sorted(scope.items()))

    @staticmethod
    def key_to_scope(key: str) -> Dict:
        """Convert a canonical string key back to a scope dict."""
        return dict(json.loads(key))

    def create_version(self, name: str, scope: Dict, config: Dict,
                       includes: List[Dict] = None, schema_ref: Dict = None) -> int:
        """Create a new config version."""
        scope_key = self.scope_to_key(scope)
        full_key = (name, scope_key)

        if full_key not in self.configs:
            self.configs[full_key] = {}

        versions = self.configs[full_key]
        new_version = max(versions.keys(), default=0) + 1

        entry = {
            "config": normalize_value(deepcopy(config)),
            "includes": includes or [],
            "schema_ref": schema_ref
        }
        versions[new_version] = entry
        return new_version

    def get_version(self, name: str, scope: Dict, version: int) -> Optional[Dict]:
        """Get a specific config version."""
        scope_key = self.scope_to_key(scope)
        full_key = (name, scope_key)

        if full_key not in self.configs:
            return None
        return self.configs[full_key].get(version)

    def get_latest_version(self, name: str, scope: Dict) -> Optional[int]:
        """Get the latest version number for a config."""
        scope_key = self.scope_to_key(scope)
        full_key = (name, scope_key)

        if full_key not in self.configs:
            return None
        versions = self.configs[full_key]
        if not versions:
            return None
        return max(versions.keys())

    def get_config_entry(self, name: str, scope: Dict, version: int = None) -> Optional[Dict]:
        """Get a config entry, optionally at a specific version."""
        if version is None:
            version = self.get_latest_version(name, scope)
            if version is None:
                return None
        return self.get_version(name, scope, version)

    def set_binding(self, name: str, scope: Dict, schema_ref: Dict) -> Dict:
        """Set a schema binding for a config identity."""
        scope_key = self.scope_to_key(scope)
        full_key = (name, scope_key)

        binding = {
            "name": name,
            "scope": deepcopy(scope),
            "schema_ref": deepcopy(schema_ref),
            "active": True
        }
        self.bindings[full_key] = binding
        return binding

    def get_binding(self, name: str, scope: Dict) -> Optional[Dict]:
        """Get the schema binding for a config identity."""
        scope_key = self.scope_to_key(scope)
        full_key = (name, scope_key)
        return self.bindings.get(full_key)

    def resolve_config(self, name: str, scope: Dict, version: int = None,
                       visited: set = None) -> Tuple[Dict, List[Dict]]:
        """Resolve a config with includes, returning (resolved_config, inheritance_chain)."""
        if visited is None:
            visited = set()

        scope_key = self.scope_to_key(scope)
        visit_key = (name, scope_key, version)
        if visit_key in visited:
            raise ValueError(f"Circular include detected: {name}")
        visited.add(visit_key)

        entry = self.get_config_entry(name, scope, version)
        if entry is None:
            raise ValueError(f"Config not found: {name}")

        config = deepcopy(entry["config"])
        includes = entry.get("includes", [])

        inheritance_chain = [{
            "name": name,
            "scope": deepcopy(scope),
            "version": version or self.get_latest_version(name, scope)
        }]

        merged = {}
        for inc in includes:
            inc_name = inc["name"]
            inc_scope = inc.get("scope", {})
            inc_version = inc.get("version")

            merged_scope = deepcopy(scope)
            merged_scope.update(inc_scope)

            inc_config, inc_chain = self.resolve_config(
                inc_name, merged_scope, inc_version, visited.copy()
            )
            inheritance_chain.extend(inc_chain)

            merged = deep_merge(merged, inc_config)

        merged = deep_merge(merged, config)

        return merged, inheritance_chain


def deep_merge(base: Dict, override: Dict) -> Dict:
    """Deep merge two dictionaries, with override values taking precedence."""
    result = deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


class ConfigServerHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the config server."""

    schemas: SchemaRegistry = None
    configs: ConfigStore = None

    def log_message(self, format, *args):
        pass

    def send_json_response(self, status: int, data: Any):
        body = json.dumps(data, separators=(',', ':')).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def send_error_response(self, status: int, code: str, message: str, details: Dict = None):
        error = {"code": code, "message": message}
        if details:
            error["details"] = details
        self.send_json_response(status, {"error": error})

    def send_validation_error(self, e: SchemaValidationError):
        self.send_error_response(422, "validation_failed",
                                "Config does not conform to schema", {
                                    "path": e.path,
                                    "rule": e.rule,
                                    "expected": e.expected,
                                    "actual": e.actual
                                })

    def resolve_effective_schema(self, name: str, scope: Dict, schema_ref: Dict = None):
        """Return (effective_schema_ref, effective_schema) or (None, None) if sent error."""
        if schema_ref:
            schema_name = schema_ref.get("name")
            schema_version = schema_ref.get("version")
            if not self.schemas.exists(schema_name, schema_version):
                self.send_error_response(404, "not_found", "Schema version not found")
                return None, None
            return schema_ref, self.schemas.get(schema_name, schema_version)
        binding = self.configs.get_binding(name, scope)
        if binding:
            ref = binding["schema_ref"]
            return ref, self.schemas.get(ref.get("name"), ref.get("version"))
        return None, None

    def read_request_body(self) -> bytes:
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length == 0:
            return b'{}'

        if content_length > 1024 * 1024:
            return None

        return self.rfile.read(content_length)

    def parse_json_body(self) -> Tuple[Optional[Dict], Optional[str]]:
        """Parse the request body as JSON."""
        body = self.read_request_body()
        if body is None:
            return None, "too_large"

        try:
            return json.loads(body.decode('utf-8')), None
        except json.JSONDecodeError:
            return None, "invalid_json"

    def parse_path(self) -> Tuple[str, List[str], Dict]:
        """Parse the request path into components."""
        parsed = urlparse(self.path)
        path_parts = [p for p in parsed.path.split('/') if p]
        query_params = parse_qs(parsed.query)
        return parsed.path, path_parts, query_params

    def do_POST(self):
        path, parts, query = self.parse_path()

        content_type = self.headers.get('Content-Type', '')
        if not content_type.startswith('application/json'):
            self.send_error_response(415, "unsupported_media_type",
                                    "Content-Type must be application/json")
            return

        body, error = self.parse_json_body()
        if error == "too_large":
            self.send_error_response(413, "too_large", "Request body too large")
            return
        if error == "invalid_json":
            self.send_error_response(400, "bad_request", "Invalid JSON body")
            return

        if body is None:
            body = {}

        try:
            self.route_post(path, parts, body)
        except Exception as e:
            self.send_error_response(500, "internal_error", str(e))

    def route_post(self, path: str, parts: List[str], body: Dict):
        if len(parts) < 3 or parts[0] != 'v1':
            self.send_error_response(404, "not_found", "Endpoint not found")
            return

        if parts[1] == 'schemas':
            self._route_schema(parts, body)
        elif parts[1] == 'configs':
            self._route_config(parts, body)
        else:
            self.send_error_response(404, "not_found", "Endpoint not found")

    def _route_schema(self, parts: List[str], body: Dict):
        if len(parts) < 3:
            self.send_error_response(404, "not_found", "Endpoint not found")
            return
        schema_name = parts[2]

        if len(parts) == 3:
            self.handle_create_schema(schema_name, body)
        elif len(parts) == 4 and parts[3] == 'versions':
            self.handle_list_schema_versions(schema_name, body)
        elif len(parts) == 4:
            try:
                version = int(parts[3])
                self.handle_get_schema_version(schema_name, version, body)
            except ValueError:
                self.send_error_response(400, "bad_request", "Invalid version number")
        else:
            self.send_error_response(404, "not_found", "Endpoint not found")

    def _route_config(self, parts: List[str], body: Dict):
        if len(parts) < 3:
            self.send_error_response(404, "not_found", "Endpoint not found")
            return
        name = parts[2]

        if name.endswith(':bind'):
            self.handle_bind_schema(name[:-5], body)
        elif name.endswith(':resolve'):
            self.handle_resolve_config(name[:-8], body)
        elif name.endswith(':validate'):
            self.handle_validate_config(name[:-9], body)
        elif len(parts) == 4 and parts[3] == 'schema':
            self.handle_get_binding(name, body)
        elif len(parts) == 4 and parts[3] == 'versions':
            self.handle_list_config_versions(name, body)
        elif len(parts) == 4:
            try:
                version = int(parts[3])
                self.handle_get_config_version(name, version, body)
            except ValueError:
                self.send_error_response(400, "bad_request", "Invalid version number")
        elif len(parts) == 3:
            self.handle_create_config(name, body)
        else:
            self.send_error_response(404, "not_found", "Endpoint not found")

    def handle_create_schema(self, schema_name: str, body: Dict):
        """Handle POST /v1/schemas/{schema_name}"""
        schema = None

        if "schema" in body:
            schema = body["schema"]
        elif "raw_schema" in body:
            raw = body["raw_schema"]
            fmt = body.get("raw_format", "json").lower()

            if fmt not in ("json", "yaml"):
                self.send_error_response(415, "unsupported_format",
                                        f"Unsupported format: {fmt}")
                return

            if len(raw.encode('utf-8')) > 1024 * 1024:
                self.send_error_response(413, "too_large", "Schema too large")
                return

            try:
                parsed = parse_raw_config(raw, fmt)
                if not isinstance(parsed, dict):
                    self.send_error_response(422, "schema_invalid",
                                            "Schema must be an object")
                    return
                schema = parsed
            except ParseError as e:
                self.send_error_response(422, "schema_invalid", str(e),
                                        {"reason": e.reason} if e.reason else None)
                return
        else:
            self.send_error_response(400, "bad_request",
                                    "Missing 'schema' or 'raw_schema' field")
            return

        try:
            check_external_refs(schema)
            jsonschema.Draft202012Validator.check_schema(schema)
        except ExternalRefError:
            self.send_error_response(422, "schema_invalid",
                                    "External $ref not allowed",
                                    {"reason": "external_ref_not_allowed"})
            return
        except jsonschema.exceptions.SchemaError as e:
            self.send_error_response(422, "schema_invalid", str(e))
            return
        except Exception as e:
            self.send_error_response(422, "schema_invalid", str(e))
            return

        try:
            version = self.schemas.create(schema_name, schema)
        except ValueError as e:
            self.send_error_response(409, "conflict", str(e))
            return

        self.send_json_response(201, {"name": schema_name, "version": version})

    def handle_list_schema_versions(self, schema_name: str, body: Dict):
        """Handle POST /v1/schemas/{schema_name}/versions"""
        versions = self.schemas.list_versions(schema_name)
        self.send_json_response(200, {"name": schema_name, "versions": versions})

    def handle_get_schema_version(self, schema_name: str, version: int, body: Dict):
        """Handle POST /v1/schemas/{schema_name}/{version}"""
        schema = self.schemas.get(schema_name, version)
        if schema is None:
            self.send_error_response(404, "not_found", "Schema version not found")
            return

        self.send_json_response(200, {
            "name": schema_name,
            "version": version,
            "schema": normalize_value(schema)
        })

    def handle_bind_schema(self, name: str, body: Dict):
        """Handle POST /v1/configs/{name}:bind"""
        scope = body.get("scope", {})
        schema_ref = body.get("schema_ref")

        if schema_ref is None:
            self.send_error_response(400, "bad_request", "Missing 'schema_ref'")
            return

        schema_name = schema_ref.get("name")
        schema_version = schema_ref.get("version")

        if not schema_name or schema_version is None:
            self.send_error_response(400, "bad_request", "Invalid 'schema_ref'")
            return

        if not self.schemas.exists(schema_name, schema_version):
            self.send_error_response(404, "not_found", "Schema version not found")
            return

        binding = self.configs.set_binding(name, scope, schema_ref)
        self.send_json_response(200, binding)

    def handle_get_binding(self, name: str, body: Dict):
        """Handle POST /v1/configs/{name}/schema"""
        scope = body.get("scope", {})
        binding = self.configs.get_binding(name, scope)

        if binding is None:
            self.send_error_response(404, "not_found", "No binding found")
            return

        response = {
            "name": binding["name"],
            "scope": binding["scope"],
            "schema_ref": binding["schema_ref"]
        }
        self.send_json_response(200, response)

    def handle_create_config(self, name: str, body: Dict):
        """Handle POST /v1/configs/{name}"""
        scope = body.get("scope", {})
        includes = body.get("includes", [])
        schema_ref = body.get("schema_ref")

        config = None

        if "config" in body:
            config = body["config"]
        elif "raw_config" in body:
            raw = body["raw_config"]
            fmt = body.get("raw_format", "json").lower()

            if fmt not in ("json", "yaml", "toml"):
                self.send_error_response(415, "unsupported_format",
                                        f"Unsupported format: {fmt}")
                return

            if len(raw.encode('utf-8')) > 1024 * 1024:
                self.send_error_response(413, "too_large", "Config too large")
                return

            try:
                parsed = parse_raw_config(raw, fmt)
                if not isinstance(parsed, dict):
                    self.send_error_response(422, "unprocessable",
                                            "Config must be a JSON object")
                    return
                config = parsed
            except ParseError as e:
                details = {"reason": e.reason} if e.reason else None
                self.send_error_response(422, "unprocessable", str(e), details)
                return
        else:
            self.send_error_response(400, "bad_request",
                                    "Missing 'config' or 'raw_config' field")
            return

        config = normalize_value(config)

        effective_schema_ref, effective_schema = self.resolve_effective_schema(name, scope, schema_ref)
        if effective_schema_ref is None and schema_ref is not None:
            return

        if effective_schema:
            try:
                validate_config_against_schema(config, effective_schema)
            except SchemaValidationError as e:
                self.send_validation_error(e)
                return

        version = self.configs.create_version(name, scope, config, includes, schema_ref)

        self.send_json_response(201, {
            "name": name,
            "scope": scope,
            "version": version
        })

    def handle_list_config_versions(self, name: str, body: Dict):
        """Handle POST /v1/configs/{name}/versions"""
        scope = body.get("scope", {})

        scope_key = self.configs.scope_to_key(scope)
        full_key = (name, scope_key)

        if full_key not in self.configs.configs:
            self.send_json_response(200, {"name": name, "scope": scope, "versions": []})
            return

        versions = sorted(self.configs.configs[full_key].keys())
        self.send_json_response(200, {"name": name, "scope": scope, "versions": versions})

    def handle_get_config_version(self, name: str, version: int, body: Dict):
        """Handle POST /v1/configs/{name}/{version}"""
        scope = body.get("scope", {})

        entry = self.configs.get_version(name, scope, version)
        if entry is None:
            self.send_error_response(404, "not_found", "Config version not found")
            return

        self.send_json_response(200, {
            "name": name,
            "scope": scope,
            "version": version,
            "config": entry["config"],
            "includes": entry.get("includes", [])
        })

    def handle_resolve_config(self, name: str, body: Dict):
        """Handle POST /v1/configs/{name}:resolve"""
        scope = body.get("scope", {})
        version = body.get("version")
        schema_ref = body.get("schema_ref")

        latest = self.configs.get_latest_version(name, scope)
        if latest is None:
            self.send_error_response(404, "not_found", "Config not found")
            return

        try:
            resolved_config, inheritance_chain = self.configs.resolve_config(
                name, scope, version
            )
        except ValueError as e:
            self.send_error_response(400, "bad_request", str(e))
            return

        effective_schema_ref, effective_schema = self.resolve_effective_schema(name, scope, schema_ref)
        if effective_schema_ref is None and schema_ref is not None:
            return

        response = {
            "name": name,
            "scope": scope,
            "resolved_config": normalize_value(resolved_config),
            "inheritance_chain": inheritance_chain
        }

        if effective_schema:
            try:
                validate_config_against_schema(resolved_config, effective_schema)
                response["validated_against"] = effective_schema_ref
            except SchemaValidationError as e:
                self.send_validation_error(e)
                return

        self.send_json_response(200, response)

    def handle_validate_config(self, name: str, body: Dict):
        scope = body.get("scope", {})
        version = body.get("version")
        schema_ref = body.get("schema_ref")
        mode = body.get("mode", "resolved")

        if mode not in ("stored", "resolved"):
            self.send_error_response(400, "bad_request", "Invalid mode")
            return

        latest = self.configs.get_latest_version(name, scope)
        if latest is None:
            self.send_error_response(404, "not_found", "Config not found")
            return

        version_used = version or latest

        entry = self.configs.get_version(name, scope, version_used)
        if entry is None:
            self.send_error_response(404, "not_found", "Config version not found")
            return

        effective_schema_ref, effective_schema = self.resolve_effective_schema(name, scope, schema_ref)
        if effective_schema_ref is None and schema_ref is not None:
            return  # Error already sent

        if effective_schema is None:
            self.send_error_response(404, "schema_not_bound", "No schema bound")
            return

        if mode == "stored":
            config_to_validate = entry["config"]
        else:
            try:
                config_to_validate, _ = self.configs.resolve_config(name, scope, version_used)
            except ValueError as e:
                self.send_error_response(400, "bad_request", str(e))
                return

        try:
            validate_config_against_schema(config_to_validate, effective_schema)
            self.send_json_response(200, {
                "name": name,
                "scope": scope,
                "version_used": version_used,
                "mode": mode,
                "valid": True,
                "validated_against": effective_schema_ref
            })
        except SchemaValidationError as e:
            self.send_validation_error(e)


def run_server(address: str = "0.0.0.0", port: int = 8080):
    ConfigServerHandler.schemas = SchemaRegistry()
    ConfigServerHandler.configs = ConfigStore()

    server = HTTPServer((address, port), ConfigServerHandler)
    print(f"Config server running on {address}:{port}", file=sys.stderr)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...", file=sys.stderr)
        server.shutdown()


def main():
    parser = argparse.ArgumentParser(description="Config Server")
    parser.add_argument("--address", default="0.0.0.0", help="Address to bind to")
    parser.add_argument("--port", type=int, default=8080, help="Port to bind to")
    args = parser.parse_args()

    run_server(args.address, args.port)


if __name__ == "__main__":
    main()
