#!/usr/bin/env python3
"""Config storage and merging logic for the Config Service."""

from dataclasses import dataclass, field
from typing import Any


class ConfigError(Exception):
    """Base exception for config service errors."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(message)


Scope = dict[str, str]
MAX_REQUEST_SIZE = 1024 * 1024
MAX_VERSIONS_PER_SCOPE = 10_000
MAX_INCLUDE_CHAIN = 64


def scope_hash(scope: Scope) -> int:
    """Hash a scope for dictionary lookup."""
    return hash(tuple(sorted(scope.items())))


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


@dataclass(frozen=True)
class IncludeRef:
    """Reference to another config."""
    name: str
    scope: Scope
    version: int | None

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


def validate_scope(d: dict[str, str]) -> dict[str, str]:
    """Validate scope dict."""
    for k, v in d.items():
        if not isinstance(k, str):
            raise ConfigError('invalid_input', f'Scope key must be string, got {type(k).__name__}')
        if not isinstance(v, str):
            raise ConfigError('invalid_input', f'Scope value must be string, got {type(v).__name__}')
    return d


@dataclass
class ConfigVersion:
    """An immutable version of a config."""
    name: str
    scope: Scope
    version: int
    config: dict[str, Any]
    includes: tuple[IncludeRef, ...] = ()
    active: bool = False
    status: str = 'draft'

    def to_dict(self, include_config: bool = True) -> dict[str, Any]:
        result = {
            'name': self.name,
            'scope': self.scope,
            'version': self.version,
            'status': self.status,
            'active': self.active,
        }
        if include_config:
            result['config'] = self.config
            result['includes'] = [inc.to_dict() for inc in self.includes]
        return result


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


class ConfigStorage:
    """In-memory storage for configs."""

    _configs: dict[tuple[str, int], list[ConfigVersion]] = field(default_factory=dict)

    def _scope_key(self, name: str, scope: Scope) -> tuple[str, int]:
        return (name, scope_hash(scope))

    def _get_versions(self, name: str, scope: Scope) -> list[ConfigVersion]:
        key = self._scope_key(name, scope)
        return self._configs.get(key, [])

    def create_version(self, name: str, scope: Scope, config: dict[str, Any],
                       includes: list[IncludeRef], inherits_active: bool = False) -> ConfigVersion:
        key = self._scope_key(name, scope)
        versions = self._configs.get(key, [])

        if len(versions) >= MAX_VERSIONS_PER_SCOPE:
            raise ConfigError('conflict', f'Maximum {MAX_VERSIONS_PER_SCOPE} versions reached for {name}')

        next_version = len(versions) + 1

        for v in versions:
            if v.config == config and v.includes == tuple(includes):
                if not v.active:
                    object.__setattr__(v, 'active', True)
                return v

        if inherits_active and versions:
            active = versions[-1]
            config = deep_merge(active.config, config)

        for v in versions:
            object.__setattr__(v, 'active', False)

        new_version = ConfigVersion(
            name=name,
            scope=scope,
            version=next_version,
            config=config,
            includes=tuple(includes),
            active=False,
            status='draft'
        )

        self._configs[key] = versions + [new_version]
        return new_version

    def get_version(self, name: str, scope: Scope, version: int) -> ConfigVersion:
        versions = self._get_versions(name, scope)
        for v in versions:
            if v.version == version:
                return v
        raise ConfigError('not_found', f'Version {version} not found for {name}')

    def get_active(self, name: str, scope: Scope) -> ConfigVersion:
        versions = self._get_versions(name, scope)
        for v in reversed(versions):
            if v.active:
                return v
        raise ConfigError('not_found', f'No active version for {name}')

    def list_versions(self, name: str, scope: Scope) -> list[ConfigVersion]:
        return list(self._get_versions(name, scope))

    def activate_version(self, name: str, scope: Scope, version: int) -> ConfigVersion:
        versions = self._get_versions(name, scope)
        for v in versions:
            if v.version == version:
                for ver in versions:
                    object.__setattr__(ver, 'active', ver.version == version)
                return v
        raise ConfigError('not_found', f'Version {version} not found for {name}')

    def rollback(self, name: str, scope: Scope, to_version: int) -> ConfigVersion:
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
            raise ConfigError('conflict',
                f'Cannot rollback to version {to_version} (newer than active {current_active.version})')

        return self.activate_version(name, scope, to_version)


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


def compute_drafts_storage_diff(base_config: dict[str, Any],
                                 draft_config: dict[str, Any]) -> list[dict[str, Any]]:
    """Compute RFC 6902 patch between two stored configs."""
    patch = []

    base_keys = set(base_config.keys())
    draft_keys = set(draft_config.keys())

    for key in sorted(base_keys - draft_keys):
        patch.append({'op': 'remove', 'path': f'/{key}'})

    for key in sorted(draft_keys):
        if key in base_config:
            if base_config[key] != draft_config[key]:
                patch.append({'op': 'replace', 'path': f'/{key}', 'value': draft_config[key]})
        else:
            patch.append({'op': 'add', 'path': f'/{key}', 'value': draft_config[key]})

    return patch


def compute_drafts_resolved_diff(base_resolved: dict[str, Any],
                                  draft_resolved: dict[str, Any]) -> list[dict[str, Any]]:
    """Compute RFC 6902 patch between two resolved configs."""
    def deep_diff(base, draft, path=''):
        diff = []

        if isinstance(base, dict) and isinstance(draft, dict):
            for key in sorted(set(base.keys()) - set(draft.keys())):
                diff.append({'op': 'remove', 'path': f'{path}/{key}' if path else f'/{key}'})

            for key in sorted(set(draft.keys()) - set(base.keys())):
                diff.append({'op': 'add', 'path': f'{path}/{key}' if path else f'/{key}', 'value': draft[key]})

            for key in sorted(set(base.keys()) & set(draft.keys())):
                if base[key] != draft[key]:
                    diff.extend(deep_diff(base[key], draft[key], f'{path}/{key}' if path else f'/{key}'))
        elif isinstance(base, list) and isinstance(draft, list):
            if base != draft:
                diff.append({'op': 'replace', 'path': path, 'value': draft})
        elif base != draft:
            diff.append({'op': 'replace', 'path': path, 'value': draft})

        return diff

    patch = deep_diff(base_resolved, draft_resolved)

    op_order = {'remove': 0, 'replace': 1, 'add': 2}
    patch.sort(key=lambda x: (x['path'], op_order.get(x['op'], 3)))

    return patch


def compute_includes_changes(base_includes: tuple[IncludeRef, ...],
                              draft_includes: tuple[IncludeRef, ...]) -> list[dict[str, Any]]:
    """Compute changes to the includes list."""
    changes = []

    base_dict = {i: ref for i, ref in enumerate(base_includes)}
    draft_dict = {i: ref for i, ref in enumerate(draft_includes)}

    for idx in sorted(set(base_dict.keys()) - set(draft_dict.keys())):
        changes.append({
            'op': 'remove',
            'index': idx,
            'ref': base_dict[idx].to_dict()
        })

    for idx in sorted(set(draft_dict.keys()) - set(base_dict.keys())):
        changes.append({
            'op': 'add',
            'index': idx,
            'ref': draft_dict[idx].to_dict()
        })

    for idx in sorted(set(base_dict.keys()) & set(draft_dict.keys())):
        base_ref = base_dict[idx]
        draft_ref = draft_dict[idx]
        if base_ref.version != draft_ref.version:
            changes.append({
                'op': 'update',
                'index': idx,
                'from_version': base_ref.version,
                'to_version': draft_ref.version,
                'ref': draft_ref.to_dict()
            })

    return changes


def get_nested_value(config: dict[str, Any], path: str) -> Any:
    """Get a value from a nested dict using a JSON Pointer path."""
    if path == '/':
        return config

    parts = path.strip('/').split('/')
    current = config

    for part in parts:
        part = part.replace('~1', '/').replace('~0', '~')
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None

    return current
