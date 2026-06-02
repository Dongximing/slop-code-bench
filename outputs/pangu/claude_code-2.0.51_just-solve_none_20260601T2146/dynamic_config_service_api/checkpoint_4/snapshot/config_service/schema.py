#!/usr/bin/env python3
"""Schema management and validation for the Config Service."""

from dataclasses import dataclass
from typing import Any

# For JSON Schema validation
from jsonschema import Draft202012Validator

# Constants
MAX_SCHEMA_VERSIONS_PER_NAME = 1000


class ConfigError(Exception):
    """Base exception for config service errors."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(message)


@dataclass(frozen=True)
class SchemaVersion:
    """An immutable version of a JSON Schema."""
    name: str
    version: int
    schema: dict[str, Any]
    raw_source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            'name': self.name,
            'version': self.version,
            'schema': self.schema,
        }


class SchemaStorage:
    """In-memory storage for schemas."""

    def __init__(self):
        self._schemas: dict[str, list[SchemaVersion]] = {}

    def create_version(self, name: str, schema: dict[str, Any], raw_source: str | None = None) -> SchemaVersion:
        if name not in self._schemas:
            self._schemas[name] = []

        versions = self._schemas[name]

        if len(versions) >= MAX_SCHEMA_VERSIONS_PER_NAME:
            raise ConfigError('conflict', f'Maximum {MAX_SCHEMA_VERSIONS_PER_NAME} versions reached for schema {name}')

        next_version = len(versions) + 1

        new_schema = SchemaVersion(
            name=name,
            version=next_version,
            schema=schema,
            raw_source=raw_source
        )

        versions.append(new_schema)
        return new_schema

    def get_version(self, name: str, version: int) -> SchemaVersion:
        if name not in self._schemas:
            raise ConfigError('not_found', f'Schema {name} not found')

        for sv in self._schemas[name]:
            if sv.version == version:
                return sv

        raise ConfigError('not_found', f'Schema version {version} not found for {name}')

    def list_versions(self, name: str) -> list[SchemaVersion]:
        if name not in self._schemas:
            raise ConfigError('not_found', f'Schema {name} not found')
        return list(self._schemas[name])

    def get_latest(self, name: str) -> SchemaVersion | None:
        if name not in self._schemas or not self._schemas[name]:
            return None
        return self._schemas[name][-1]


def validate_schema_against_itself(schema_doc: dict[str, Any]) -> tuple[bool, str | None]:
    """Validate that a document is a valid JSON Schema Draft 2020-12."""
    if not isinstance(schema_doc, dict):
        return False, 'Schema must be a JSON object'

    if '$ref' in schema_doc:
        ref_value = schema_doc['$ref']
        if isinstance(ref_value, str) and (ref_value.startswith('http://') or ref_value.startswith('https://')):
            return False, 'External $ref not allowed'

    if '$dynamicRef' in schema_doc:
        ref_value = schema_doc.get('$dynamicRef', '')
        if isinstance(ref_value, str) and (ref_value.startswith('http://') or ref_value.startswith('https://')):
            return False, 'External $dynamicRef not allowed'

    return True, None


def _traverse_nested(obj, path, on_dict, on_list, on_scalar=None):
    """Generic recursive traversal over nested dicts/lists."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            cp = f'{path}/{key}' if path else f'/{key}'
            if on_dict(key, value, cp) is False:
                continue
            _traverse_nested(value, cp, on_dict, on_list, on_scalar)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            cp = f'{path}/{i}'
            on_list(i, item, cp)
            _traverse_nested(item, cp, on_dict, on_list, on_scalar)
    elif on_scalar:
        on_scalar(obj, path)


def check_for_external_refs(obj, path=''):
    """Check for external $ref/$dynamicRef in schema."""
    def on_dict(key, value, current_path):
        if key == '$ref' and isinstance(value, str):
            if value.startswith('http://') or value.startswith('https://'):
                raise ConfigError('schema_invalid', 'External $ref not allowed',
                                {'reason': 'external_ref_not_allowed'})
        elif key == '$dynamicRef' and isinstance(value, str):
            if value.startswith('http://') or value.startswith('https://'):
                raise ConfigError('schema_invalid', 'External $dynamicRef not allowed',
                                {'reason': 'external_ref_not_allowed'})
    _traverse_nested(obj, path, on_dict, lambda i, item, p: None)


def validate_against_schema(instance: dict[str, Any], schema_doc: dict[str, Any]) -> tuple[bool, dict[str, str] | None]:
    """Validate an instance against a JSON Schema."""
    try:
        validator = Draft202012Validator(schema_doc)
        errors = list(validator.iter_errors(instance))
        if not errors:
            return True, None

        best_error = None
        best_path = None

        for error in errors:
            path = error.json_path or '/'
            rule = error.validator
            details = {'path': path, 'rule': rule}

            if rule == 'type':
                expected = error.validator_value
                expected = expected[0] if isinstance(expected, list) and expected else str(expected)
                details['expected'] = str(expected)
                details['actual'] = type(error.instance).__name__
            elif rule == 'enum':
                details['expected'] = 'one of ' + str(error.validator_value)
                details['actual'] = str(error.instance)
            elif rule == 'required':
                details['expected'] = 'required property ' + str(error.validator_value)
                details['actual'] = 'missing'
            elif rule == 'pattern':
                details['expected'] = f'pattern {error.validator_value}'
                details['actual'] = str(error.instance)[:50] + ('...' if len(str(error.instance)) > 50 else '')
            elif rule in ('minimum', 'maximum', 'multipleOf'):
                details['expected'] = f'{rule} {error.validator_value}'
                details['actual'] = str(error.instance)
            elif rule in ('minLength', 'maxLength'):
                details['expected'] = f'{rule} {error.validator_value}'
                details['actual'] = f'length {len(str(error.instance))}'
            elif rule in ('minProperties', 'maxProperties'):
                details['expected'] = f'{rule} {error.validator_value}'
                details['actual'] = f'{len(error.instance)} properties'
            else:
                details['expected'] = str(error.validator_value)[:100]
                details['actual'] = str(error.instance)[:100]

            if best_path is None or path < best_path:
                best_path = path
                best_error = details

        return False, best_error
    except Exception as e:
        return False, {'path': '/', 'rule': 'schema_error', 'expected': 'valid JSON Schema', 'actual': str(e)}
