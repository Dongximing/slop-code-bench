#!/usr/bin/env python3
"""Schema binding management for the Config Service."""

from dataclasses import dataclass
from typing import Any


class ConfigError(Exception):
    """Base exception for config service errors."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(message)


Scope = dict[str, str]


def scope_hash(scope: Scope) -> int:
    """Hash a scope for dictionary lookup."""
    return hash(tuple(sorted(scope.items())))


@dataclass(frozen=True)
class Binding:
    """A binding associating a config identity with a schema."""
    name: str
    scope: Scope
    schema_name: str
    schema_version: int
    active: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            'name': self.name,
            'scope': self.scope,
            'schema_ref': {
                'name': self.schema_name,
                'version': self.schema_version,
            },
            'active': self.active,
        }


class BindingStorage:
    """In-memory storage for schema bindings."""

    def __init__(self):
        self._bindings: dict[tuple[str, int], Binding] = {}

    def _key(self, name: str, scope: Scope) -> tuple[str, int]:
        return (name, scope_hash(scope))

    def bind(self, name: str, scope: Scope, schema_name: str, schema_version: int) -> Binding:
        """Create or update a binding."""
        key = self._key(name, scope)
        binding = Binding(
            name=name,
            scope=scope,
            schema_name=schema_name,
            schema_version=schema_version,
            active=True
        )
        self._bindings[key] = binding
        return binding

    def get_binding(self, name: str, scope: Scope) -> Binding | None:
        """Get the binding for a (name, scope) pair."""
        key = self._key(name, scope)
        return self._bindings.get(key)

    def get_effective_schema(self, name: str, scope: Scope, schema_storage,
                             override_schema_ref: dict[str, int] | None = None):
        """Get the effective schema for a config identity.

        Returns a tuple of (schema_version, schema_ref_used).
        """
        if override_schema_ref is not None:
            schema_name = override_schema_ref.get('name')
            schema_version_num = override_schema_ref.get('version')
            if schema_name and schema_version_num is not None:
                try:
                    schema_ver = schema_storage.get_version(schema_name, schema_version_num)
                    return schema_ver, override_schema_ref
                except ConfigError:
                    raise ConfigError('not_found', f'Schema {schema_name} version {schema_version_num} not found')

        binding = self.get_binding(name, scope)
        if binding is None:
            return None, None

        try:
            schema_ver = schema_storage.get_version(binding.schema_name, binding.schema_version)
            schema_ref = {'name': binding.schema_name, 'version': binding.schema_version}
            return schema_ver, schema_ref
        except ConfigError:
            return None, None
