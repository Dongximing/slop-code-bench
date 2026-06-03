#!/usr/bin/env python3
"""
Configuration Management Service
A REST service for storing JSON configuration objects with immutable versions,
scoping, rollback, imports/inheritance, schema registry, and validation.
"""

import argparse
import json
import re
import sys
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from flask import Flask, request
import yaml
import toml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError as JsonSchemaError

app = Flask(__name__)

# =============================================================================
# Constants
# =============================================================================
MAX_RAW_SIZE = 1024 * 1024  # 1 MiB
MAX_SCHEMA_VERSIONS = 1000

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


@dataclass(frozen=True)
class Scope:
    """Represents a scope as a flat string-to-string map."""
    items: Tuple[Tuple[str, str], ...]

    def __init__(self, scope_dict: Dict[str, str] = None):
        if scope_dict is None:
            scope_dict = {}
        for k, v in scope_dict.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise ConfigError("invalid_input", f"Scope keys and values must be strings, got {type(k).__name__}/{type(v).__name__}")
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
        if version is not None and (not isinstance(version, int) or version < 1):
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
# Schema Registry
# =============================================================================

@dataclass
class SchemaVersion:
    """An immutable version of a schema."""
    schema: Dict[str, Any]  # The parsed JSON Schema object
    raw_content: str  # Original raw content for storage
    created_at: float = field(default_factory=lambda: datetime.now().timestamp())


class SchemaStorage:
    """In-memory storage for schemas with versioning."""

    def __init__(self):
        # Key: schema_name -> list of (version, SchemaVersion)
        self._schemas: Dict[str, List[Tuple[int, SchemaVersion]]] = {}

    def create_version(self, name: str, schema_obj: Dict, raw_content: str) -> int:
        """Create a new schema version. Returns version number."""
        if name not in self._schemas:
            self._schemas[name] = []

        if len(self._schemas[name]) >= MAX_SCHEMA_VERSIONS:
            raise ConfigError("conflict", f"Maximum schema versions ({MAX_SCHEMA_VERSIONS}) exceeded for {name}",
                            status=409)

        new_version = len(self._schemas[name]) + 1
        entry = SchemaVersion(
            schema=schema_obj,
            raw_content=raw_content,
            created_at=datetime.now().timestamp()
        )
        self._schemas[name].append((new_version, entry))
        return new_version

    def get_version(self, name: str, version: int) -> Optional[SchemaVersion]:
        """Get a specific schema version."""
        if name not in self._schemas:
            return None
        for v, entry in self._schemas[name]:
            if v == version:
                return entry
        return None

    def list_versions(self, name: str) -> List[Dict]:
        """List all versions for a schema name."""
        if name not in self._schemas:
            return []
        return [{"version": v, "created_at": entry.created_at} for v, entry in self._schemas[name]]

    def get_latest(self, name: str) -> Optional[Tuple[int, SchemaVersion]]:
        """Get the latest version of a schema."""
        if name not in self._schemas or not self._schemas[name]:
            return None
        return self._schemas[name][-1]


# =============================================================================
# Schema Binding
# =============================================================================

@dataclass(frozen=True)
class SchemaRef:
    """Reference to a specific schema version."""
    name: str
    version: int

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name:
            raise ConfigError("invalid_input", "Schema name must be a non-empty string")
        if not isinstance(self.version, int) or self.version < 1:
            raise ConfigError("invalid_input", "Schema version must be a positive integer")

    def to_dict(self) -> Dict:
        return {"name": self.name, "version": self.version}


class BindingStorage:
    """Storage for schema bindings."""

    def __init__(self):
        # Key: (config_name, scope) -> SchemaRef
        self._bindings: Dict[Tuple[str, Scope], SchemaRef] = {}

    def bind(self, config_name: str, scope: Scope, schema_ref: SchemaRef) -> None:
        """Create or update a binding."""
        self._bindings[(config_name, scope)] = schema_ref

    def get_binding(self, config_name: str, scope: Scope) -> Optional[SchemaRef]:
        """Get the binding for a config name and scope."""
        return self._bindings.get((config_name, scope))

    def get_binding_by_schema(self, schema_name: str, schema_version: int) -> List[Dict]:
        """Get all bindings for a specific schema version."""
        result = []
        for (config_name, scope), schema_ref in self._bindings.items():
            if schema_ref.name == schema_name and schema_ref.version == schema_version:
                result.append({
                    "name": config_name,
                    "scope": scope.as_dict(),
                    "schema_ref": schema_ref.to_dict()
                })
        return result


# =============================================================================
# Raw Config Parsing
# =============================================================================

def parse_raw_config(raw_content: str, fmt: str) -> Dict[str, Any]:
    """
    Parse raw configuration string into a JSON object.
    Supports json, yaml, and toml formats.
    """
    fmt = fmt.lower()

    if fmt == 'json':
        # Strict JSON parsing - no trailing commas, no comments
        try:
            # Use json module directly - it's strict by default
            obj = json.loads(raw_content)
        except json.JSONDecodeError as e:
            raise ConfigError("unprocessable", f"Invalid JSON: {e}",
                            details={"reason": "invalid_json"})

    elif fmt == 'yaml':
        # YAML 1.2 with restrictions
        try:
            # Parse and check for disallowed features
            # yaml.safe_load won't parse custom tags or allow arbitrary Python objects
            # but we need to check for anchors/aliases and merge keys
            # Parse line by line to check for patterns that indicate unsafe features
            lines = raw_content.split('\n')

            for line in lines:
                stripped = line.strip()
                # Skip comments
                if stripped.startswith('#'):
                    continue
                # Check for explicit tags (!!str, !!int, etc.)
                if '!!' in stripped:
                    raise ConfigError("unprocessable", "YAML feature not allowed",
                                    details={"reason": "yaml_feature_not_allowed"})
                # Check for merge key (<<)
                if stripped == '<<':
                    raise ConfigError("unprocessable", "YAML feature not allowed",
                                    details={"reason": "yaml_feature_not_allowed"})
                # Check for anchor reference (*anchor) - but allow as comment or in string
                # Simple heuristic: if it starts with * and isn't in quotes
                if stripped.startswith('*') and not stripped.startswith(\"*'\") and not stripped.startswith('"*'):
                    raise ConfigError("unprocessable", "YAML feature not allowed",
                                    details={"reason": "yaml_feature_not_allowed"})

            obj = yaml.safe_load(raw_content)
        except yaml.YAMLError as e:
            raise ConfigError("unprocessable", f"Invalid YAML: {e}",
                            details={"reason": "invalid_yaml"})

    elif fmt == 'toml':
        # TOML 1.0 with JSON-only value restriction
        try:
            obj = toml.loads(raw_content)
        except toml.TomlDecodeError as e:
            raise ConfigError("unprocessable", f"Invalid TOML: {e}",
                            details={"reason": "invalid_toml"})

        # Check for non-JSON-native types
        def check_json_types(value: Any, path: str = ""):
            if isinstance(value, dict):
                for k, v in value.items():
                    check_json_types(v, f"{path}/{k}" if path else f"/{k}")
            elif isinstance(value, list):
                for i, item in enumerate(value):
                    check_json_types(item, f"{path}[{i}]")
            elif value is None:
                pass  # null is allowed
            elif isinstance(value, (str, int, float, bool)):
                # Check for datetime-like strings that should be explicit
                if isinstance(value, str):
                    # Basic ISO datetime pattern check
                    isodatetime_pattern = r'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}'
                    if re.match(isodatetime_pattern, value):
                        # This might be a datetime - it's allowed as a string
                        pass
            else:
                raise ConfigError("unprocessable", f"Non-JSON type at {path}",
                                details={"reason": "non_json_type"})

        try:
            check_json_types(obj)
        except ConfigError:
            raise

    else:
        raise ConfigError("unsupported_format", f"Unsupported format: {fmt}",
                        details={"reason": "unknown_format"})

    # Root must be a JSON object (dict)
    if not isinstance(obj, dict):
        raise ConfigError("unprocessable", "Config root must be a JSON object",
                        details={"reason": "root_not_object"})

    return obj


# =============================================================================
# JSON Schema Validation
# =============================================================================

def validate_against_schema(config: Dict, schema_obj: Dict) -> Optional[Dict]:
    """
    Validate config against JSON Schema.
    Returns error details if validation fails, None if valid.
    """
    try:
        validator = Draft202012Validator(schema_obj)
    except JsonSchemaError as e:
        raise ConfigError("schema_invalid", f"Invalid schema: {e}",
                        details={"reason": "schema_validation_error"})

    errors = list(validator.iter_errors(config))

    if not errors:
        return None

    # Return the lexicographically smallest JSON Pointer path
    best_error = None
    best_path = None

    for error in errors:
        # Get the JSON Pointer path
        path = error.absolute_path
        if path:
            # Convert to JSON Pointer format
            pointer = "/" + "/".join(str(part).replace('~', '~0').replace('/', '~1') for part in path)
        else:
            pointer = ""

        if best_path is None or pointer < best_path:
            best_path = pointer
            best_error = error

    if best_error is None:
        best_error = errors[0]
        best_path = ""

    # Determine the rule that was violated
    rule = "unknown"
    if best_error.validator == 'type':
        rule = 'type'
    elif best_error.validator == 'enum':
        rule = 'enum'
    elif best_error.validator == 'required':
        rule = 'required'
    elif best_error.validator == 'pattern':
        rule = 'pattern'
    elif best_error.validator == 'minLength':
        rule = 'minLength'
    elif best_error.validator == 'maxLength':
        rule = 'maxLength'
    elif best_error.validator == 'minimum':
        rule = 'minimum'
    elif best_error.validator == 'maximum':
        rule = 'maximum'
    elif best_error.validator == 'minProperties':
        rule = 'minProperties'
    elif best_error.validator == 'maxProperties':
        rule = 'maxProperties'
    elif best_error.validator == 'minItems':
        rule = 'minItems'
    elif best_error.validator == 'maxItems':
        rule = 'maxItems'
    elif best_error.validator == 'properties':
        rule = 'additionalProperties' if best_error.validator_value is False else 'properties'
    elif best_error.validator == 'additionalProperties':
        rule = 'additionalProperties'
    elif best_error.validator == 'oneOf':
        rule = 'oneOf'
    elif best_error.validator == 'anyOf':
        rule = 'anyOf'
    elif best_error.validator == 'allOf':
        rule = 'allOf'
    elif best_error.validator == '$ref':
        rule = '$ref'

    # Get expected and actual values
    expected = None
    actual = None

    if best_error.validator == 'type':
        expected = best_error.validator_value
        if isinstance(expected, list):
            expected = expected
        actual = type(config).__name__ if not best_path else None
        # Try to get actual type from instance
        if best_path:
            instance = best_error.instance
            actual = type(instance).__name__ if instance is not None else 'null'
    elif best_error.validator == 'required':
        expected = best_error.validator_value
    elif best_error.validator == 'enum':
        expected = best_error.validator_value
    elif best_error.validator == 'pattern':
        expected = best_error.validator_value
    elif best_error.validator == 'minimum':
        expected = best_error.validator_value
    elif best_error.validator == 'maximum':
        expected = best_error.validator_value

    details = {
        "path": best_path,
        "rule": rule,
        "expected": expected,
        "actual": actual
    }

    return details


def check_schema_for_external_refs(schema_obj: Dict) -> None:
    """Check that schema doesn't contain external $refs."""
    def check_refs(obj: Any, path: str = ""):
        if isinstance(obj, dict):
            if '$ref' in obj:
                ref_value = obj['$ref']
                # Allow only in-document refs (starting with # or not a URL)
                if not ref_value.startswith('#'):
                    raise ConfigError("schema_invalid", "External $ref not allowed",
                                    details={"reason": "external_ref_not_allowed",
                                           "ref": ref_value})
            for k, v in obj.items():
                check_refs(v, f"{path}/{k}" if path else f"/{k}")
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                check_refs(item, f"{path}[{i}]")

    check_refs(schema_obj)


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

    MAX_VERSIONS = 10_000

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
# Resolution
# =============================================================================

@dataclass
class ResolutionResult:
    """Result of a config resolution."""
    resolved_config: Dict
    resolution_graph: List[Dict]
    version_used: int


class ConfigResolver:
    """Resolves configurations with includes and cycle detection."""

    def __init__(self, storage: ConfigStorage, schema_storage: SchemaStorage,
                 binding_storage: BindingStorage):
        self.storage = storage
        self.schema_storage = schema_storage
        self.binding_storage = binding_storage

    def resolve(self, name: str, scope: Scope, version: Optional[int] = None,
                dry_run: bool = False,
                schema_ref_override: Optional[SchemaRef] = None) -> ResolutionResult:
        """
        Resolve a configuration with all includes applied.
        If version is None, uses the active version.
        If dry_run is True, doesn't require the config to exist.
        schema_ref_override: optional schema reference to validate against
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
        self.schema_storage = SchemaStorage()
        self.binding_storage = BindingStorage()
        self.resolver = ConfigResolver(self.storage, self.schema_storage, self.binding_storage)
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

        scope = Scope(scope_dict)

        parsed_includes = [IncludeRef.from_dict(inc) for inc in includes]

        # Validate config
        if not isinstance(config, dict):
            raise ConfigError("invalid_input", "Config must be a JSON object")

        version, is_active = self.storage.create_version(
            name, scope, config, parsed_includes, inherits_active
        )

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

    def resolve(self, name: str, scope_dict: Dict, version: Optional[int],
                dry_run: bool, schema_ref_override: Optional[Dict] = None) -> Tuple:
        """Resolve a configuration with includes."""
        try:
            scope = Scope(scope_dict)
        except Exception as e:
            return error_response("invalid_input", f"Invalid scope: {e}")

        try:
            # Determine effective schema
            effective_schema_ref = None

            if schema_ref_override:
                # Use override if provided
                effective_schema_ref = SchemaRef(**schema_ref_override)
                # Verify the schema exists
                schema_entry = self.schema_storage.get_version(
                    effective_schema_ref.name, effective_schema_ref.version
                )
                if schema_entry is None:
                    return error_response("not_found",
                                        f"Schema {effective_schema_ref.name} version {effective_schema_ref.version} not found",
                                        status=404)
            else:
                # Try to get active binding
                effective_schema_ref = self.binding_storage.get_binding(name, scope)

            result = self.resolver.resolve(name, scope, version, dry_run, effective_schema_ref)

            # Validate if schema is available
            validated_against = None
            if effective_schema_ref:
                schema_entry = self.schema_storage.get_version(
                    effective_schema_ref.name, effective_schema_ref.version
                )
                if schema_entry:
                    validation_error = validate_against_schema(result.resolved_config, schema_entry.schema)
                    if validation_error:
                        return (
                            json.dumps({
                                "error": {
                                    "code": "validation_failed",
                                    "message": "Config does not conform to schema",
                                    "details": validation_error
                                }
                            }, separators=(',', ':')) + '\n',
                            422,
                            {'Content-Type': 'application/json; charset=utf-8'}
                        )
                    validated_against = effective_schema_ref.to_dict()

            response = {
                "name": name,
                "scope": scope_dict,
                "version_used": result.version_used,
                "resolved_config": result.resolved_config,
                "resolution_graph": result.resolution_graph
            }
            if validated_against:
                response["validated_against"] = validated_against

            return (
                json.dumps(response, separators=(',', ':'), ensure_ascii=False) + '\n',
                200,
                {'Content-Type': 'application/json; charset=utf-8'}
            )
        except ConfigError as e:
            return e.args[0], e.status, {'Content-Type': 'application/json; charset=utf-8'}
        except Exception as e:
            return error_response("internal", f"Resolution failed: {e}")

    # =============================================================================
    # Schema Endpoints
    # =============================================================================

    def create_schema(self, name: str, body: Dict) -> Tuple:
        """Create a new schema version."""
        # Check if using structured or raw format
        if 'schema' in body:
            # Structured JSON Schema
            schema_obj = body['schema']
            raw_content = json.dumps(schema_obj, separators=(',', ':'))
        elif 'raw_schema' in body:
            # Raw schema string
            raw_schema = body['raw_schema']
            raw_format = body.get('raw_format', 'json')

            if not isinstance(raw_schema, str):
                raise ConfigError("invalid_input", "raw_schema must be a string")

            if len(raw_schema) > MAX_RAW_SIZE:
                raise ConfigError("too_large", f"Raw schema exceeds {MAX_RAW_SIZE} bytes", status=413)

            if raw_format == 'json':
                try:
                    schema_obj = json.loads(raw_schema)
                except json.JSONDecodeError as e:
                    raise ConfigError("schema_invalid", f"Invalid JSON schema: {e}",
                                    details={"reason": "invalid_json"})
            elif raw_format == 'yaml':
                try:
                    schema_obj = yaml.safe_load(raw_schema)
                except yaml.YAMLError as e:
                    raise ConfigError("schema_invalid", f"Invalid YAML schema: {e}",
                                    details={"reason": "invalid_yaml"})
            else:
                raise ConfigError("unsupported_format", f"Unsupported raw format: {raw_format}")

            raw_content = raw_schema
        else:
            raise ConfigError("invalid_input", "Either 'schema' or 'raw_schema' must be provided")

        # Validate it's a dict
        if not isinstance(schema_obj, dict):
            raise ConfigError("invalid_input", "Schema must be a JSON object")

        # Check for external refs
        check_schema_for_external_refs(schema_obj)

        # Validate schema using jsonschema
        try:
            Draft202012Validator.check_schema(schema_obj)
        except JsonSchemaError as e:
            raise ConfigError("schema_invalid", f"Invalid JSON Schema: {e}",
                            details={"reason": "schema_validation_error"})

        # Create version
        version = self.schema_storage.create_version(name, schema_obj, raw_content)

        return (
            json.dumps({
                "name": name,
                "version": version
            }, separators=(',', ':')) + '\n',
            201,
            {'Content-Type': 'application/json; charset=utf-8'}
        )

    def list_schema_versions(self, name: str, body: Dict) -> Tuple:
        """List all versions of a schema."""
        versions = self.schema_storage.list_versions(name)
        return (
            json.dumps({
                "name": name,
                "versions": versions
            }, separators=(',', ':')) + '\n',
            200,
            {'Content-Type': 'application/json; charset=utf-8'}
        )

    def get_schema_version(self, name: str, version: int, body: Dict) -> Tuple:
        """Get a specific schema version."""
        entry = self.schema_storage.get_version(name, version)
        if entry is None:
            return error_response("not_found", f"Schema {name} version {version} not found", status=404)

        return (
            json.dumps({
                "name": name,
                "version": version,
                "schema": entry.schema,
                "created_at": entry.created_at
            }, separators=(',', ':'), ensure_ascii=False) + '\n',
            200,
            {'Content-Type': 'application/json; charset=utf-8'}
        )

    def bind_schema(self, name: str, body: Dict) -> Tuple:
        """Bind a schema to a config identity."""
        if 'scope' not in body:
            raise ConfigError("invalid_input", "Missing required field: scope")
        if 'schema_ref' not in body:
            raise ConfigError("invalid_input", "Missing required field: schema_ref")

        scope_dict = body['scope']
        schema_ref_data = body['schema_ref']

        if not isinstance(scope_dict, dict):
            raise ConfigError("invalid_input", "Scope must be an object")
        if not isinstance(schema_ref_data, dict):
            raise ConfigError("invalid_input", "schema_ref must be an object")

        try:
            schema_ref = SchemaRef(
                name=schema_ref_data['name'],
                version=schema_ref_data['version']
            )
        except (KeyError, TypeError) as e:
            raise ConfigError("invalid_input", f"Invalid schema_ref: {e}")

        # Verify the schema version exists
        schema_entry = self.schema_storage.get_version(schema_ref.name, schema_ref.version)
        if schema_entry is None:
            return error_response("not_found",
                                f"Schema {schema_ref.name} version {schema_ref.version} not found",
                                status=404)

        # Create binding
        scope = Scope(scope_dict)
        self.binding_storage.bind(name, scope, schema_ref)

        return (
            json.dumps({
                "name": name,
                "scope": scope_dict,
                "schema_ref": schema_ref.to_dict(),
                "active": True
            }, separators=(',', ':')) + '\n',
            200,
            {'Content-Type': 'application/json; charset=utf-8'}
        )

    def get_schema_ref(self, name: str, body: Dict) -> Tuple:
        """Get the active schema binding for a config identity."""
        if 'scope' not in body:
            raise ConfigError("invalid_input", "Missing required field: scope")

        scope_dict = body['scope']
        if not isinstance(scope_dict, dict):
            raise ConfigError("invalid_input", "Scope must be an object")

        scope = Scope(scope_dict)
        schema_ref = self.binding_storage.get_binding(name, scope)

        if schema_ref is None:
            return error_response("not_found",
                                f"No schema bound for ({name}, {scope_dict})",
                                status=404)

        return (
            json.dumps({
                "name": name,
                "scope": scope_dict,
                "schema_ref": schema_ref.to_dict()
            }, separators=(',', ':')) + '\n',
            200,
            {'Content-Type': 'application/json; charset=utf-8'}
        )

    def validate_config(self, name: str, body: Dict) -> Tuple:
        """Validate a config without modifying state."""
        if 'scope' not in body:
            raise ConfigError("invalid_input", "Missing required field: scope")

        scope_dict = body['scope']
        if not isinstance(scope_dict, dict):
            raise ConfigError("invalid_input", "Scope must be an object")

        version = body.get('version')
        if version is not None and (not isinstance(version, int) or version < 1):
            raise ConfigError("invalid_input", "version must be a positive integer or null")

        schema_ref_override = body.get('schema_ref')
        if schema_ref_override is not None:
            if not isinstance(schema_ref_override, dict):
                raise ConfigError("invalid_input", "schema_ref must be an object")
            if 'name' not in schema_ref_override or 'version' not in schema_ref_override:
                raise ConfigError("invalid_input", "schema_ref must have 'name' and 'version'")

        mode = body.get('mode', 'resolved')
        if mode not in ('stored', 'resolved'):
            raise ConfigError("invalid_input", "mode must be 'stored' or 'resolved'")

        scope = Scope(scope_dict)

        # Determine effective schema
        effective_schema_ref = None

        if schema_ref_override:
            effective_schema_ref = SchemaRef(**schema_ref_override)
            schema_entry = self.schema_storage.get_version(
                effective_schema_ref.name, effective_schema_ref.version
            )
            if schema_entry is None:
                return error_response("not_found",
                                    f"Schema {effective_schema_ref.name} version {effective_schema_ref.version} not found",
                                    status=404)
        else:
            effective_schema_ref = self.binding_storage.get_binding(name, scope)

        if effective_schema_ref is None:
            return error_response("not_found",
                                f"No schema found for ({name}, {scope_dict})",
                                status=404)

        # Get the schema
        schema_entry = self.schema_storage.get_version(
            effective_schema_ref.name, effective_schema_ref.version
        )
        if schema_entry is None:
            return error_response("not_found",
                                f"Schema {effective_schema_ref.name} version {effective_schema_ref.version} not found",
                                status=404)

        # Get the config to validate
        if mode == 'stored':
            if version is None:
                entry = self.storage.get_active(name, scope)
                if entry is None:
                    return error_response("not_found",
                                        f"No active config for ({name}, {scope_dict})",
                                        status=404)
                version = entry.version
            else:
                entry = self.storage.get_version(name, scope, version)
                if entry is None:
                    return error_response("not_found",
                                        f"Version {version} not found for ({name}, {scope_dict})",
                                        status=404)

            config_to_validate = entry.config
            mode_label = "stored"
        else:  # resolved
            try:
                result = self.resolver.resolve(name, scope, version, False, effective_schema_ref)
                config_to_validate = result.resolved_config
                mode_label = "resolved"
                version_used = result.version_used
            except ConfigError as e:
                return e.args[0], e.status, {'Content-Type': 'application/json; charset=utf-8'}

        # Validate
        validation_error = validate_against_schema(config_to_validate, schema_entry.schema)

        response = {
            "name": name,
            "scope": scope_dict,
            "version_used": version if version else (version_used if mode == 'resolved' else None),
            "mode": mode_label,
            "valid": validation_error is None,
            "validated_against": effective_schema_ref.to_dict()
        }

        if validation_error:
            response["error"] = {
                "code": "validation_failed",
                "message": "Config does not conform to schema",
                "details": validation_error
            }
            return (
                json.dumps(response, separators=(',', ':'), ensure_ascii=False) + '\n',
                422,
                {'Content-Type': 'application/json; charset=utf-8'}
            )

        return (
            json.dumps(response, separators=(',', ':'), ensure_ascii=False) + '\n',
            200,
            {'Content-Type': 'application/json; charset=utf-8'}
        )


# =============================================================================
# Flask App Routes
# =============================================================================

service = ConfigService()


def parse_json_body(max_size: int = MAX_RAW_SIZE) -> Dict:
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


# =============================================================================
# Config Endpoints (Part 1)
# =============================================================================

@app.route('/v1/configs/<name>', methods=['POST'])
def create_config(name):
    """Create a new config version. Supports raw_config/raw_format."""
    try:
        body = parse_json_body()

        # Validate required fields
        if 'scope' not in body:
            raise ConfigError("invalid_input", "Missing required field: scope")
        if 'config' not in body and 'raw_config' not in body:
            raise ConfigError("invalid_input", "Either 'config' or 'raw_config' must be provided")

        scope = body['scope']
        includes = body.get('includes', [])
        inherits_active = body.get('inherits_active', False)
        schema_ref_override = body.get('schema_ref')

        if not isinstance(scope, dict):
            raise ConfigError("invalid_input", "Scope must be an object")
        if not isinstance(includes, list):
            raise ConfigError("invalid_input", "Includes must be a list")
        if not isinstance(inherits_active, bool):
            raise ConfigError("invalid_input", "inherits_active must be a boolean")

        # Parse config (either structured or raw)
        if 'config' in body:
            config = body['config']
            if not isinstance(config, dict):
                raise ConfigError("invalid_input", "Config must be a JSON object")
        else:
            # Parse raw config
            raw_config = body['raw_config']
            raw_format = body.get('raw_format', 'json')

            if not isinstance(raw_config, str):
                raise ConfigError("invalid_input", "raw_config must be a string")

            if len(raw_config) > MAX_RAW_SIZE:
                raise ConfigError("too_large", f"Raw config exceeds {MAX_RAW_SIZE} bytes", status=413)

            config = parse_raw_config(raw_config, raw_format)

        # Check idempotency - normalize body for comparison
        # For raw config, we use the parsed config for the body hash
        body_for_hash = {
            "scope": scope,
            "config": config,
            "includes": includes,
            "inherits_active": inherits_active
        }
        if schema_ref_override is not None:
            body_for_hash['schema_ref'] = schema_ref_override

        body_json = canonical_json(body_for_hash).strip()

        if service.idempotency.check(name, body_json):
            # Already exists - return existing active version info
            scope_obj = Scope(scope)
            store = service.storage.get_store(name, scope_obj)
            if store and store.get_active():
                active = store.get_active()
                return (
                    json.dumps({
                        "name": name,
                        "scope": scope,
                        "version": active.version,
                        "active": True
                    }, separators=(',', ':'), ensure_ascii=False) + '\n',
                    201,
                    {'Content-Type': 'application/json; charset=utf-8'}
                )

        scope_obj = Scope(scope)
        parsed_includes = [IncludeRef.from_dict(inc) for inc in includes]

        # Validate config is a dict (already ensured by parse_raw_config for raw)
        if not isinstance(config, dict):
            raise ConfigError("invalid_input", "Config must be a JSON object")

        # Determine effective schema for validation
        effective_schema_ref = None
        if schema_ref_override:
            effective_schema_ref = SchemaRef(**schema_ref_override)
        else:
            effective_schema_ref = service.binding_storage.get_binding(name, scope_obj)

        # Validate if schema exists
        if effective_schema_ref:
            schema_entry = service.schema_storage.get_version(
                effective_schema_ref.name, effective_schema_ref.version
            )
            if schema_entry:
                validation_error = validate_against_schema(config, schema_entry.schema)
                if validation_error:
                    return (
                        json.dumps({
                            "error": {
                                "code": "validation_failed",
                                "message": "Config does not conform to schema",
                                "details": validation_error
                            }
                        }, separators=(',', ':')) + '\n',
                        422,
                        {'Content-Type': 'application/json; charset=utf-8'}
                    )

        version, is_active = service.storage.create_version(
            name, scope_obj, config, parsed_includes, inherits_active
        )

        service.idempotency.record(name, body_json)

        return (
            json.dumps({
                "name": name,
                "scope": scope,
                "version": version,
                "active": is_active
            }, separators=(',', ':'), ensure_ascii=False) + '\n',
            201,
            {'Content-Type': 'application/json; charset=utf-8'}
        )

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
    """Resolve a configuration with includes and validate against bound schema."""
    try:
        body = parse_json_body()
        if 'scope' not in body:
            raise ConfigError("invalid_input", "Missing required field: scope")

        version = body.get('version')
        if version is not None and (not isinstance(version, int) or version < 1):
            raise ConfigError("invalid_input", "version must be a positive integer or null")

        dry_run = body.get('dry_run', False)
        if not isinstance(dry_run, bool):
            raise ConfigError("invalid_input", "dry_run must be a boolean")

        schema_ref_override = body.get('schema_ref')
        if schema_ref_override is not None:
            if not isinstance(schema_ref_override, dict):
                raise ConfigError("invalid_input", "schema_ref must be an object")
            if 'name' not in schema_ref_override or 'version' not in schema_ref_override:
                raise ConfigError("invalid_input", "schema_ref must have 'name' and 'version'")

        return service.resolve(name, body['scope'], version, dry_run, schema_ref_override)
    except ConfigError as e:
        return e.args[0], e.status, {'Content-Type': 'application/json; charset=utf-8'}
    except Exception as e:
        return error_response("internal", str(e))


# =============================================================================
# Schema Endpoints (Part 2)
# =============================================================================

@app.route('/v1/schemas/<schema_name>', methods=['POST'])
def create_schema(schema_name):
    """Create a new schema version."""
    try:
        body = parse_json_body()
        return service.create_schema(schema_name, body)
    except ConfigError as e:
        return error_response(e.code, e.message, e.details, e.status)
    except Exception as e:
        return error_response("internal", str(e))


@app.route('/v1/schemas/<schema_name>/versions', methods=['POST'])
def list_schema_versions(schema_name):
    """List all schema versions."""
    try:
        body = parse_json_body()
        return service.list_schema_versions(schema_name, body)
    except ConfigError as e:
        return error_response(e.code, e.message, e.details, e.status)
    except Exception as e:
        return error_response("internal", str(e))


@app.route('/v1/schemas/<schema_name>/<int:schema_version>', methods=['POST'])
def get_schema_version(schema_name, schema_version):
    """Get a specific schema version."""
    try:
        body = parse_json_body()
        return service.get_schema_version(schema_name, schema_version, body)
    except ConfigError as e:
        return error_response(e.code, e.message, e.details, e.status)
    except Exception as e:
        return error_response("internal", str(e))


@app.route('/v1/configs/<name>:bind', methods=['POST'])
def bind_schema(name):
    """Bind a schema to a config identity."""
    try:
        body = parse_json_body()
        return service.bind_schema(name, body)
    except ConfigError as e:
        return error_response(e.code, e.message, e.details, e.status)
    except Exception as e:
        return error_response("internal", str(e))


@app.route('/v1/configs/<name>/schema', methods=['POST'])
def get_schema_ref(name):
    """Get the active schema binding for a config identity."""
    try:
        body = parse_json_body()
        return service.get_schema_ref(name, body)
    except ConfigError as e:
        return error_response(e.code, e.message, e.details, e.status)
    except Exception as e:
        return error_response("internal", str(e))


@app.route('/v1/configs/<name>:validate', methods=['POST'])
def validate_config(name):
    """Validate a config without modifying state."""
    try:
        body = parse_json_body()
        return service.validate_config(name, body)
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


def main():
    parser = argparse.ArgumentParser(description='Configuration Management Service')
    parser.add_argument('--address', default='0.0.0.0', help='Address to bind to (default: 0.0.0.0)')
    parser.add_argument('--port', type=int, default=8080, help='Port to listen on (default: 8080)')
    args = parser.parse_args()

    app.run(host=args.address, port=args.port, debug=False)


if __name__ == '__main__':
    main()
