#!/usr/bin/env python3
"""
Configuration Server with Change-Management Workflow for Configuration Activation.
"""

import datetime
import json
import re
from typing import Any
from copy import deepcopy

from flask import Flask, request, Response

app = Flask(__name__)

# =============================================================================
# In-Memory Storage
# =============================================================================

# Config storage: (name, frozenset(scope)) -> list[ConfigVersion]
configs: dict[tuple[str, frozenset], list["ConfigVersion"]] = {}
# Active version cache: (name, frozenset(scope)) -> int
active_version_cache: dict[tuple[str, frozenset], int] = {}

# Schema registry storage
schemas: dict[tuple[str, int], dict] = {}  # (name, version) -> schema dict
schema_versions: dict[str, list[int]] = {}  # name -> sorted list of versions
schema_bindings: dict[tuple[str, frozenset], dict] = {}  # (name, scope) -> binding info

# Approval policies: (name, frozenset(scope)) -> ApprovalPolicy
approval_policies: dict[tuple[str, frozenset], "ApprovalPolicy"] = {}

# Proposals storage: proposal_id -> Proposal
proposals: dict[int, "Proposal"] = {}
# Next proposal ID (globally monotonic)
next_proposal_id = 1

# Reviews storage: proposal_id -> dict[actor] -> Review
reviews: dict[int, dict[str, "Review"]] = {}


# =============================================================================
# Constants and Limits
# =============================================================================

MAXRequestBody = 1024 * 1024  # 1 MiB
MAX_DEPTH = 64
MAX_VERSIONS = 10_000
MAX_SCHEMA_VERSIONS = 1_000
MAX_RAW_SIZE = 1024 * 1024  # 1 MiB

MAX_PROPOSALS_PER_IDENTITY = 1_000
MAX_REVIEWS_PER_PROPOSAL = 1_000
MAX_TITLE_BYTES = 200
MAX_DESCRIPTION_BYTES = 8 * 1024  # 8 KiB
MAX_LABELS = 32
MAX_LABEL_BYTES = 32
MAX_ACTOR_BYTES = 128

LABEL_PATTERN = re.compile(r'^[a-z0-9._-]+$')


# =============================================================================
# Data Classes
# =============================================================================

class ConfigVersion:
    __slots__ = ('version', 'config', 'includes', 'active', 'canonical_body',
                 'schema_ref', 'status')

    def __init__(self, version: int, config: dict, includes: list[dict],
                 active: bool = False, canonical_body: str = None,
                 schema_ref: dict = None, status: str = None):
        self.version = version
        self.config = config
        self.includes = includes
        self.active = active
        self.canonical_body = canonical_body
        self.schema_ref = schema_ref
        self.status = status or ('active' if active else 'draft')


class ApprovalPolicy:
    __slots__ = ('scope', 'required_approvals', 'allow_author_approval',
                 'allowed_reviewers')

    def __init__(self, scope: dict, required_approvals: int = 2,
                 allow_author_approval: bool = False,
                 allowed_reviewers: list[str] = None):
        self.scope = scope
        self.required_approvals = required_approvals
        self.allow_author_approval = allow_author_approval
        self.allowed_reviewers = allowed_reviewers

    def to_dict(self) -> dict:
        return {
            'scope': self.scope,
            'required_approvals': self.required_approvals,
            'allow_author_approval': self.allow_author_approval,
            'allowed_reviewers': self.allowed_reviewers
        }


class Review:
    __slots__ = ('actor', 'decision', 'message')

    def __init__(self, actor: str, decision: str, message: str = None):
        self.actor = actor
        self.decision = decision
        self.message = message

    def to_dict(self) -> dict:
        return {
            'actor': self.actor,
            'decision': self.decision,
            'message': self.message
        }


class Tally:
    __slots__ = ('approvals', 'rejections', 'by_actor')

    def __init__(self):
        self.approvals = set()
        self.rejections = set()
        self.by_actor: dict[str, str] = {}

    def to_dict(self) -> dict:
        return {
            'approvals': sorted(self.approvals),
            'rejections': sorted(self.rejections),
            'by_actor': dict(sorted(self.by_actor.items()))
        }


class DiffArtifacts:
    __slots__ = ('raw_json_patch', 'resolved_json_patch',
                 'includes_changes', 'human')

    def __init__(self):
        self.raw_json_patch: list[dict] = []
        self.resolved_json_patch: list[dict] = []
        self.includes_changes: list[dict] = []
        self.human: list[str] = []

    def to_dict(self) -> dict:
        return {
            'raw_json_patch': self.raw_json_patch,
            'resolved_json_patch': self.resolved_json_patch,
            'includes_changes': self.includes_changes,
            'human': sorted(self.human)
        }


class Proposal:
    __slots__ = ('proposal_id', 'name', 'scope', 'draft_version', 'base_version',
                 'author', 'title', 'description', 'labels', 'status',
                 'quorum', 'tally', 'diffs', 'created_at')

    def __init__(self, proposal_id: int, name: str, scope: dict,
                 draft_version: int, base_version: int, author: str,
                 title: str = None, description: str = None,
                 labels: list[str] = None, quorum: ApprovalPolicy = None,
                 diffs: DiffArtifacts = None):
        self.proposal_id = proposal_id
        self.name = name
        self.scope = scope
        self.draft_version = draft_version
        self.base_version = base_version
        self.author = author
        self.title = title
        self.description = description
        self.labels = sorted(labels) if labels else []
        self.status = 'open'
        self.quorum = quorum
        self.tally = Tally()
        self.diffs = diffs or DiffArtifacts()
        self.created_at = datetime.datetime.utcnow().isoformat() + 'Z'

    def to_dict(self) -> dict:
        return {
            'proposal_id': self.proposal_id,
            'name': self.name,
            'scope': self.scope,
            'draft_version': self.draft_version,
            'base_version': self.base_version,
            'author': self.author,
            'title': self.title,
            'description': self.description,
            'labels': self.labels,
            'status': self.status,
            'quorum': {
                'required_approvals': self.quorum.required_approvals,
                'allow_author_approval': self.quorum.allow_author_approval,
                'allowed_reviewers': self.quorum.allowed_reviewers
            },
            'tally': self.tally.to_dict(),
            'diffs': self.diffs.to_dict(),
            'created_at': self.created_at
        }


# =============================================================================
# Parsing and Validation Helpers (from original, extended)
# =============================================================================

import json
import yaml
import toml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError


def parse_raw_config(raw: str, fmt: str) -> dict:
    """Parse raw config string into canonical JSON object."""
    if fmt == 'json':
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f'Invalid JSON: {e}')
        if not isinstance(parsed, dict):
            raise ValueError('Config must be a JSON object')
        canonical = json.loads(json.dumps(parsed, sort_keys=True, ensure_ascii=False))
        return canonical

    elif fmt == 'yaml':
        class RestrictedLoader(yaml.SafeLoader):
            pass

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

        for k in parsed.keys():
            if not isinstance(k, str):
                raise ValueError('yaml_feature_not_allowed')

        canonical = json.loads(json.dumps(parsed, sort_keys=True, ensure_ascii=False))
        return canonical

    elif fmt == 'toml':
        try:
            parsed = toml.loads(raw)
        except toml.TomlDecodeError as e:
            raise ValueError(f'Invalid TOML: {e}')

        if not isinstance(parsed, dict):
            raise ValueError('Config must be a TOML table (object)')

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
            check_json_types(parsed)
        except ValueError as e:
            if 'non_json_type' in str(e):
                raise
            raise

        canonical = json.loads(json.dumps(parsed, sort_keys=True, ensure_ascii=False))
        return canonical

    else:
        raise ValueError('unsupported_format')


def validate_schema_for_refs(schema: dict, parent_path: str = ''):
    """Validate that $ref only references within the same document."""
    if isinstance(schema, dict):
        if '$ref' in schema:
            ref = schema['$ref']
            if ref.startswith('http://') or ref.startswith('https://'):
                raise ValueError('external_ref_not_allowed')
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

        sorted_errors = sorted(errors, key=lambda e: str(e.absolute_path))
        first_error = sorted_errors[0]

        path = '/' + '/'.join(str(p) for p in first_error.absolute_path) if first_error.absolute_path else '/'
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

        return {'path': path, 'rule': rule, 'expected': expected, 'actual': actual}
    except Exception:
        return None


def get_effective_schema(name: str, scope: dict, schema_ref: dict = None) -> dict | None:
    """Get effective schema for a (name, scope) pair with precedence."""
    if schema_ref is not None:
        s_name = schema_ref.get('name')
        s_version = schema_ref.get('version')
        if s_name and s_version:
            key = (s_name, s_version)
            if key in schemas:
                return schemas[key]
    else:
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


# =============================================================================
# Response Helpers
# =============================================================================

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


# =============================================================================
# Validation Helpers
# =============================================================================

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

    return {'name': name, 'scope': scope, 'version': version}


def validate_actor(actor: Any) -> str:
    """Validate an actor string."""
    if not isinstance(actor, str) or len(actor) == 0:
        raise ValueError('actor must be a non-empty string')
    if len(actor.encode('utf-8')) > MAX_ACTOR_BYTES:
        raise ValueError(f'actor exceeds {MAX_ACTOR_BYTES} bytes')
    return actor


def validate_label(label: Any) -> str:
    """Validate a single label."""
    if not isinstance(label, str):
        raise ValueError('label must be a string')
    if len(label) == 0:
        raise ValueError('label cannot be empty')
    if len(label.encode('utf-8')) > MAX_LABEL_BYTES:
        raise ValueError(f'label exceeds {MAX_LABEL_BYTES} bytes')
    if not LABEL_PATTERN.match(label):
        raise ValueError(f'label "{label}" does not match pattern [a-z0-9._-]')
    return label


# =============================================================================
# Deep Merge and Config Resolution (from original, extended)
# =============================================================================

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

            if isinstance(existing, dict) and isinstance(value, dict):
                result[key] = deep_merge(existing, value, current_path)
            elif isinstance(existing, list) or isinstance(value, list):
                result[key] = value if isinstance(value, list) else value
            else:
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

    visit_key = (name, scope_fs, version)
    if visit_key in visited:
        raise ValueError('cycle_detected')

    if len(visited) > MAX_DEPTH:
        raise ValueError('max_depth')

    if key not in configs or not configs[key]:
        raise ValueError('not_found')

    versions = configs[key]

    target_version = None
    if version is None:
        if key not in active_version_cache:
            raise ValueError('not_found')
        target_version = active_version_cache[key]
    else:
        for v in versions:
            if v.version == version:
                target_version = version
                break
        if target_version is None:
            raise ValueError('not_found')

    target_config = None
    for v in versions:
        if v.version == target_version:
            target_config = v
            break

    if target_config is None:
        raise ValueError('not_found')

    new_visited = visited | {visit_key}

    resolved = {}
    graph = [{
        'name': name,
        'scope': denormalize_scope(scope_fs),
        'version_used': target_version
    }]

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

        for node in inc_graph:
            node_key = (node['name'], frozenset(node['scope'].items()),
                       node['version_used'])
            if node_key not in new_visited:
                graph.append(node)
                new_visited |= {node_key}

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


# =============================================================================
# Diff Generation (RFC 6902 JSON Patch)
# =============================================================================

def generate_json_patch(base: dict, target: dict, path: str = '') -> list[dict]:
    """
    Generate RFC 6902 JSON Patch operations from base to target.
    Returns sorted list of operations following RFC 6902 ordering.
    """
    patch = []

    all_keys = sorted(set(base.keys()) | set(target.keys()))

    for key in all_keys:
        current_path = f'{path}/{key}' if path else f'/{key}'

        key_in_base = key in base
        key_in_target = key in target

        if not key_in_base and key_in_target:
            # ADD - new key in target
            patch.append({'op': 'add', 'path': current_path, 'value': target[key]})
        elif key_in_base and not key_in_target:
            # REMOVE - key removed from target
            patch.append({'op': 'remove', 'path': current_path})
        else:
            # Both have the key
            base_val = base[key]
            target_val = target[key]

            if isinstance(base_val, dict) and isinstance(target_val, dict):
                # Recursively patch nested objects
                nested_patch = generate_json_patch(base_val, target_val, current_path)
                patch.extend(nested_patch)
            elif isinstance(base_val, list) or isinstance(target_val, list):
                # Arrays replace entirely
                if base_val != target_val:
                    patch.append({'op': 'replace', 'path': current_path, 'value': target_val})
            else:
                # Scalars: replace if different
                if base_val != target_val:
                    patch.append({'op': 'replace', 'path': current_path, 'value': target_val})

    # Sort: remove, replace, add (lexicographically by path within each group)
    def sort_key(op):
        path = op['path']
        op_order = {'remove': 0, 'replace': 1, 'add': 2}
        return (op_order[op['op']], path)

    patch.sort(key=sort_key)
    return patch


def generate_includes_changes(old_includes: list[dict], new_includes: list[dict]) -> list[dict]:
    """
    Generate includes changes between two include lists.
    Returns list of op/index/ref entries.
    """
    changes = []

    # Create lookup by key (name, scope frozen)
    def make_key(ref):
        return (ref['name'], frozenset(ref['scope'].items()))

    old_by_key = {make_key(r): r for r in old_includes}
    new_by_key = {make_key(r): r for r in new_includes}

    # Get all keys sorted
    all_keys = sorted(set(old_by_key.keys()) | set(new_by_key.keys()))

    for key in all_keys:
        in_old = key in old_by_key
        in_new = key in new_by_key

        if not in_old and in_new:
            # Added - find index
            ref = new_by_key[key]
            # Need to find the index in new list
            for idx, r in enumerate(new_includes):
                if make_key(r) == key:
                    changes.append({
                        'op': 'add',
                        'index': idx,
                        'ref': r
                    })
                    break
        elif in_old and not in_new:
            # Removed - find index in old
            ref = old_by_key[key]
            for idx, r in enumerate(old_includes):
                if make_key(r) == key:
                    changes.append({
                        'op': 'remove',
                        'index': idx,
                        'ref': r
                    })
                    break
        elif in_old and in_new:
            # Updated - check if version changed
            old_ref = old_by_key[key]
            new_ref = new_by_key[key]
            if old_ref['version'] != new_ref['version']:
                for idx, r in enumerate(new_includes):
                    if make_key(r) == key:
                        changes.append({
                            'op': 'update',
                            'index': idx,
                            'from_version': old_ref['version'],
                            'to_version': new_ref['version']
                        })
                        break

    # Sort by index ascending
    changes.sort(key=lambda x: x['index'])
    return changes


def generate_human_diffs(raw_patch: list[dict], includes_changes: list[dict],
                         base_config: dict, target_config: dict) -> list[str]:
    """
    Generate human-readable diff descriptions.
    """
    human = []

    for op in raw_patch:
        op_type = op['op']
        path = op['path']

        if op_type == 'remove':
            human.append(f'DELETE {path}')
        elif op_type == 'replace':
            value = op['value']
            human.append(f'REPLACE {path}: {json.dumps(value, sort_keys=True, ensure_ascii=False)}')
        elif op_type == 'add':
            value = op['value']
            human.append(f'SET {path}: {json.dumps(value, sort_keys=True, ensure_ascii=False)}')

    for change in includes_changes:
        op = change['op']
        idx = change['index']
        ref = change['ref']
        scope_json = json.dumps(ref['scope'], sort_keys=True, ensure_ascii=False)

        if op == 'add':
            human.append(f'INCLUDE_ADD [{idx}] {ref["name"]}@{scope_json} v={ref["version"]}')
        elif op == 'remove':
            human.append(f'INCLUDE_REMOVE [{idx}] {ref["name"]}@{scope_json} v={ref["version"]}')
        elif op == 'update':
            from_v = change['from_version']
            to_v = change['to_version']
            human.append(f'INCLUDE_UPDATE [{idx}] {ref["name"]}@{scope_json}: {from_v} -> {to_v}')

    # Sort: DELETE, REPLACE, SET, then INCLUDE_*
    def sort_key(line):
        if line.startswith('DELETE'):
            return (0, line)
        elif line.startswith('REPLACE'):
            return (1, line)
        elif line.startswith('SET'):
            return (2, line)
        elif line.startswith('INCLUDE_ADD'):
            return (3, line)
        elif line.startswith('INCLUDE_REMOVE'):
            return (4, line)
        elif line.startswith('INCLUDE_UPDATE'):
            return (5, line)
        return (6, line)

    human.sort(key=sort_key)
    return human


def compute_diffs(draft_version: ConfigVersion, base_version: ConfigVersion) -> DiffArtifacts:
    """
    Compute all diff artifacts between draft and base versions.
    """
    diffs = DiffArtifacts()

    # Raw JSON patch (stored configs)
    diffs.raw_json_patch = generate_json_patch(
        base_version.config,
        draft_version.config
    )

    # Includes changes
    diffs.includes_changes = generate_includes_changes(
        base_version.includes,
        draft_version.includes
    )

    # For resolved patch, we need to resolve both configs
    # But we can't resolve if there are cycles. In practice, these should be
    # resolvable since they're both valid configs.
    try:
        # For base version resolution
        base_key = (base_version_config.name if hasattr(base_version, 'name') else '',
                   frozenset())  # We'll handle this differently
        base_resolved, _ = resolve_config_for_diff(base_version)
        draft_resolved, _ = resolve_config_for_diff(draft_version)
        diffs.resolved_json_patch = generate_json_patch(base_resolved, draft_resolved)
    except Exception:
        # If resolution fails, use empty patch
        diffs.resolved_json_patch = []

    # Human diffs
    diffs.human = generate_human_diffs(
        diffs.raw_json_patch,
        diffs.includes_changes,
        base_version.config,
        draft_version.config
    )

    return diffs


def resolve_config_for_diff(cv: ConfigVersion) -> tuple[dict, list]:
    """
    Resolve a config version for diff computation.
    Note: This is a simplified version - in production, you'd need the context
    (name, scope) to properly resolve includes.
    """
    # Start with empty and apply includes then own config
    resolved = {}
    graph = []

    for inc in cv.includes:
        # We can't fully resolve without the full context
        # For diff purposes, we track the include reference as-is
        pass

    # Just do a simple deep merge of own config for now
    resolved = deepcopy(cv.config)
    return resolved, graph


# =============================================================================
# Quorum Evaluation
# =============================================================================

def evaluate_quorum(proposal: Proposal) -> str:
    """
    Evaluate proposal status based on current tallies and quorum policy.
    Returns the new status.
    """
    # Check for rejections first
    if proposal.tally.rejections:
        return 'rejected'

    # Check if approvals meet required threshold
    distinct_approvals = len(proposal.tally.approvals)
    if distinct_approvals >= proposal.quorum.required_approvals:
        return 'approved'

    return 'open'


def check_review_policy(proposal: Proposal, actor: str, decision: str) -> bool:
    """
    Check if a review satisfies the policy.
    Returns True if valid, raises ValueError if violation.
    """
    # Check if actor is in allowed_reviewers (if set)
    if proposal.quorum.allowed_reviewers is not None:
        if actor not in proposal.quorum.allow_reviewers:
            raise ValueError('policy_violation')

    # Check author self-approval
    if decision == 'approve' and actor == proposal.author:
        if not proposal.quorum.allow_author_approval:
            raise ValueError('policy_violation')

    return True


# =============================================================================
# Proposal Lifecycle Management
# =============================================================================

def close_other_open_proposals(name: str, scope: dict, except_proposal_id: int = None):
    """
    Close all other open proposals for the same (name, scope) as superseded.
    """
    scope_fs = normalize_scope(scope)
    key = (name, scope_fs)

    for pid, proposal in proposals.items():
        if pid == except_proposal_id:
            continue
        if proposal.name == name and proposal.scope == scope:
            if proposal.status == 'open' or proposal.status == 'approved':
                proposal.status = 'superseded'


def supersede_proposal_for_draft(draft_version_key: tuple, new_proposal_id: int):
    """
    Supersede all open proposals for the same draft version.
    """
    for pid, proposal in proposals.items():
        if pid == new_proposal_id:
            continue
        # Check if this proposal is for the same draft
        # We need to get the config version key
        prop_key = (proposal.name, frozenset(proposal.scope.items()))
        if prop_key == draft_version_key:
            if proposal.draft_version:
                # Find the actual version object
                if prop_key in configs:
                    for cv in configs[prop_key]:
                        if cv.version == proposal.draft_version:
                            if cv.status == 'draft':
                                if proposal.status in ('open', 'approved', 'rejected'):
                                    proposal.status = 'superseded'
                            break


# =============================================================================
# Main API Endpoints
# =============================================================================

# -----------------------------------------------------------------------------
# Health Check
# -----------------------------------------------------------------------------

@app.route('/healthz', methods=['GET'])
def healthz():
    """Health check endpoint."""
    return success_response({'ok': True})


# -----------------------------------------------------------------------------
# Schema Endpoints (from original)
# -----------------------------------------------------------------------------

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

    if request.content_length and request.content_length > MAX_RAW_SIZE:
        return error_response(
            'too_large',
            'Request body exceeds 1 MiB limit',
            status=413
        )

    schema_dict = body.get('schema')
    raw_schema = body.get('raw_schema')
    raw_format = body.get('raw_format')

    if schema_dict is not None:
        if not isinstance(schema_dict, dict):
            return error_response(
                'invalid_input',
                'schema must be an object',
                status=400
            )
        schema = schema_dict
    elif raw_schema is not None and raw_format is not None:
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

    schema_key = (s_name, s_version)
    if schema_key not in schemas:
        return error_response(
            'not_found',
            f'Schema {s_name} version {s_version} not found',
            status=404
        )

    scope_fs = normalize_scope(scope)
    binding_key = (name, scope_fs)

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

    target_version = version
    if target_version is None:
        if key not in active_version_cache:
            return error_response(
                'not_found',
                f'No active config found for name={name}, scope={scope}',
                status=404
            )
        target_version = active_version_cache[key]

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

    effective_schema = get_effective_schema(name, scope, schema_ref)
    validated_against = None

    if effective_schema is not None:
        if schema_ref:
            validated_against = schema_ref
        else:
            binding = schema_bindings.get(key)
            if binding:
                validated_against = binding['schema_ref']

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


# -----------------------------------------------------------------------------
# Approval Policy Endpoints
# -----------------------------------------------------------------------------

@app.route('/v1/configs/<name>:policy', methods=['POST'])
def manage_policy(name: str):
    """Set or get approval policy for a (name, scope)."""
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

    scope = body.get('scope')
    if scope is None:
        return error_response(
            'invalid_input',
            'scope is required',
            status=400
        )

    try:
        scope = validate_scope(scope)
    except ValueError as e:
        return error_response('invalid_input', str(e), status=400)

    scope_fs = normalize_scope(scope)
    key = (name, scope_fs)

    # Check if this is a get or set operation
    # If body only contains scope, it's a get
    # If body contains policy fields, it's a set

    required_approvals = body.get('required_approvals')
    allow_author_approval = body.get('allow_author_approval')
    allowed_reviewers = body.get('allowed_reviewers')

    if required_approvals is None and allow_author_approval is None and allowed_reviewers is None:
        # GET operation - return current policy or defaults
        if key in approval_policies:
            policy = approval_policies[key]
            return success_response(policy.to_dict())
        else:
            # Return defaults
            default_policy = ApprovalPolicy(scope, 2, False, None)
            return success_response(default_policy.to_dict())

    # SET operation
    try:
        # Validate required_approvals
        if required_approvals is not None:
            if not isinstance(required_approvals, int):
                return error_response(
                    'policy_violation',
                    'required_approvals must be an integer',
                    status=422
                )
            if required_approvals < 1 or required_approvals > 10:
                return error_response(
                    'policy_violation',
                    'required_approvals must be in range [1, 10]',
                    status=422
                )
        else:
            required_approvals = 2  # default

        # Validate allow_author_approval
        if allow_author_approval is not None:
            if not isinstance(allow_author_approval, bool):
                return error_response(
                    'policy_violation',
                    'allow_author_approval must be a boolean',
                    status=422
                )
        else:
            allow_author_approval = False  # default

        # Validate allowed_reviewers
        if allowed_reviewers is not None:
            if not isinstance(allowed_reviewers, list):
                return error_response(
                    'policy_violation',
                    'allowed_reviewers must be an array',
                    status=422
                )
            for actor in allowed_reviewers:
                validate_actor(actor)
        # else: None means any actor is allowed

    except ValueError as e:
        return error_response('policy_violation', str(e), status=422)

    # Create and store policy
    policy = ApprovalPolicy(
        scope=scope,
        required_approvals=required_approvals,
        allow_author_approval=allow_author_approval,
        allowed_reviewers=allowed_reviewers
    )
    approval_policies[key] = policy

    return success_response(policy.to_dict())


# -----------------------------------------------------------------------------
# Config Version Endpoints (modified for draft workflow)
# -----------------------------------------------------------------------------

@app.route('/v1/configs/<name>', methods=['POST'])
def create_config(name: str):
    """Create a new version of a config as a draft."""
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

    if request.content_length and request.content_length > MAXRequestBody:
        return error_response(
            'too_large',
            'Request body exceeds 1 MiB limit',
            status=413
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

    try:
        scope = validate_scope(body.get('scope'))
        raw_config = body.get('raw_config')
        raw_format = body.get('raw_format')
        config = body.get('config')

        if raw_config is not None and raw_format is not None:
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
                elif 'Invalid JSON' in error_msg or 'Invalid YAML' in error_msg or 'Invalid TOML' in error_msg:
                    return error_response(
                        'unprocessable',
                        error_msg,
                        status=422
                    )
                else:
                    return error_response(
                        'invalid_input',
                        error_msg,
                        status=400
                    )
        elif config is not None:
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

        error_details = validate_against_schema(config, effective_schema)
        if error_details is not None:
            return error_response(
                'validation_failed',
                'Config does not conform to schema',
                details=error_details,
                status=422
            )

    # Canonicalize body for idempotency check
    canonical_body_dict = {
        'scope': scope,
        'config': config,
        'includes': includes
    }
    if schema_ref_override:
        canonical_body_dict['schema_ref'] = schema_ref_override
    canonical_body = json.dumps(canonical_body_dict, sort_keys=True, ensure_ascii=False)

    # Check idempotency
    existing_versions = configs.get(key, [])
    for v in existing_versions:
        if v.canonical_body == canonical_body:
            return success_response({
                'name': name,
                'scope': scope,
                'version': v.version,
                'status': v.status,
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
            for v in existing_versions:
                if v.version == active_version and v.active:
                    for k, val in v.config.items():
                        if k not in config:
                            config[k] = val
                    if not includes_raw:
                        includes = v.includes[:]
                    break

    # Create new draft version
    new_version = ConfigVersion(
        version=next_version,
        config=config,
        includes=includes,
        active=False,  # Drafts are NOT active by default
        canonical_body=canonical_body,
        schema_ref=validated_against,
        status='draft'
    )

    configs[key].append(new_version)

    response_data = {
        'name': name,
        'scope': scope,
        'version': next_version,
        'status': 'draft',
        'active': False
    }
    if validated_against:
        response_data['validated_against'] = validated_against

    return success_response(response_data, status=201)


# -----------------------------------------------------------------------------
# Proposal Endpoints
# -----------------------------------------------------------------------------

@app.route('/v1/configs/<name>/<int:version>:propose', methods=['POST'])
def create_proposal(name: str, version: int):
    """Create a new proposal for a draft version."""
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

    try:
        scope = validate_scope(body.get('scope'))
        author = body.get('author')
        validate_actor(author)
        base_version = body.get('base_version')
        if not isinstance(base_version, int) or base_version < 1:
            raise ValueError('base_version must be a positive integer')
        title = body.get('title')
        if title is not None:
            if not isinstance(title, str):
                raise ValueError('title must be a string')
            if len(title.encode('utf-8')) > MAX_TITLE_BYTES:
                raise ValueError(f'title exceeds {MAX_TITLE_BYTES} bytes')
        description = body.get('description')
        if description is not None:
            if not isinstance(description, str):
                raise ValueError('description must be a string')
            if len(description.encode('utf-8')) > MAX_DESCRIPTION_BYTES:
                raise ValueError(f'description exceeds {MAX_DESCRIPTION_BYTES} bytes')
        labels = body.get('labels', [])
        if not isinstance(labels, list):
            raise ValueError('labels must be an array')
        if len(labels) > MAX_LABELS:
            raise ValueError(f'labels exceeds {MAX_LABELS} items')
        labels = [validate_label(l) for l in labels]
    except ValueError as e:
        return error_response('invalid_input', str(e), status=400)

    scope_fs = normalize_scope(scope)
    key = (name, scope_fs)

    # Find the draft version
    draft_config = None
    if key in configs:
        for v in configs[key]:
            if v.version == version:
                draft_config = v
                break

    if draft_config is None:
        return error_response(
            'conflict',
            f'Version {version} not found for name={name}, scope={scope}',
            status=409
        )

    if draft_config.status != 'draft':
        return error_response(
            'conflict',
            f'Version {version} is not a draft',
            status=409
        )

    # Check stale_base
    current_active = active_version_cache.get(key)
    if current_active != base_version:
        return error_response(
            'stale_base',
            f'Proposal base_version {base_version} does not match current active version {current_active}',
            status=409
        )

    # Get the base config version
    base_config = None
    if key in configs:
        for v in configs[key]:
            if v.version == base_version:
                base_config = v
                break

    if base_config is None:
        return error_response(
            'not_found',
            f'Base version {base_version} not found',
            status=404
        )

    # Validate both configs against schema
    effective_schema = get_effective_schema(name, scope, draft_config.schema_ref or base_config.schema_ref)
    if effective_schema is not None:
        # Validate draft config
        error_details = validate_against_schema(draft_config.config, effective_schema)
        if error_details is not None:
            return error_response(
                'validation_failed',
                'Draft config does not conform to schema',
                details=error_details,
                status=422
            )
        # Validate base config
        error_details = validate_against_schema(base_config.config, effective_schema)
        if error_details is not None:
            return error_response(
                'validation_failed',
                'Base config does not conform to schema',
                details=error_details,
                status=422
            )

    # Get or create policy
    if key in approval_policies:
        policy = approval_policies[key]
    else:
        policy = ApprovalPolicy(scope, 2, False, None)

    # Compute diffs
    diffs = DiffArtifacts()
    diffs.raw_json_patch = generate_json_patch(base_config.config, draft_config.config)
    diffs.includes_changes = generate_includes_changes(base_config.includes, draft_config.includes)
    diffs.human = generate_human_diffs(diffs.raw_json_patch, diffs.includes_changes,
                                       base_config.config, draft_config.config)

    # Try to resolve for resolved_json_patch
    try:
        base_resolved, _ = resolve_config(name, scope, base_version)
        draft_resolved, _ = resolve_config(name, scope, version)
        diffs.resolved_json_patch = generate_json_patch(base_resolved, draft_resolved)
    except Exception:
        diffs.resolved_json_patch = []

    # Check proposal limit
    proposals_for_identity = [p for p in proposals.values()
                              if p.name == name and p.scope == scope]
    if len(proposals_for_identity) >= MAX_PROPOSALS_PER_IDENTITY:
        return error_response(
            'conflict',
            f'Maximum {MAX_PROPOSALS_PER_IDENTITY} proposals for (name, scope)',
            status=409
        )

    # Create proposal
    global next_proposal_id
    proposal_id = next_proposal_id
    next_proposal_id += 1

    proposal = Proposal(
        proposal_id=proposal_id,
        name=name,
        scope=scope,
        draft_version=version,
        base_version=base_version,
        author=author,
        title=title,
        description=description,
        labels=labels,
        quorum=policy,
        diffs=diffs
    )

    proposals[proposal_id] = proposal
    reviews[proposal_id] = {}

    # Supersede any open proposals for the same draft
    supersede_proposal_for_draft(key, proposal_id)

    return success_response(proposal.to_dict(), status=201)


@app.route('/v1/configs/<name>/proposals:list', methods=['POST'])
def list_proposals(name: str):
    """List proposals for a config identity."""
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
        status_filter = body.get('status', 'any')
        if status_filter not in ('open', 'approved', 'rejected', 'merged',
                                  'withdrawn', 'superseded', 'any'):
            return error_response(
                'invalid_input',
                'status must be one of: open, approved, rejected, merged, withdrawn, superseded, any',
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

    # Filter proposals
    result = []
    for pid in sorted(proposals.keys()):
        p = proposals[pid]
        if p.name == name and p.scope == scope:
            if status_filter == 'any' or p.status == status_filter:
                result.append(p.to_dict())

    return success_response({'proposals': result})


@app.route('/v1/proposals/<int:proposal_id>:get', methods=['POST'])
def get_proposal(proposal_id: int):
    """Fetch a proposal by ID."""
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

    if proposal_id not in proposals:
        return error_response(
            'not_found',
            f'Proposal {proposal_id} not found',
            status=404
        )

    return success_response(proposals[proposal_id].to_dict())


@app.route('/v1/proposals/<int:proposal_id>:review', methods=['POST'])
def submit_review(proposal_id: int):
    """Submit or update a review for a proposal."""
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
        actor = body.get('actor')
        validate_actor(actor)
        decision = body.get('decision')
        if decision not in ('approve', 'reject'):
            raise ValueError('decision must be "approve" or "reject"')
        message = body.get('message')
        if message is not None and not isinstance(message, str):
            raise ValueError('message must be a string')
    except ValueError as e:
        return error_response('invalid_input', str(e), status=400)

    if proposal_id not in proposals:
        return error_response(
            'not_found',
            f'Proposal {proposal_id} not found',
            status=404
        )

    proposal = proposals[proposal_id]

    # If closed, return stored state without changes
    if proposal.status in ('merged', 'withdrawn', 'superseded'):
        return success_response(proposal.to_dict())

    # Check policy
    try:
        check_review_policy(proposal, actor, decision)
    except ValueError as e:
        return error_response('policy_violation', str(e), status=422)

    # Update or create review
    proposal_reviews = reviews[proposal_id]

    old_decision = None
    if actor in proposal_reviews:
        old_decision = proposal_reviews[actor].decision

    # Create new review
    new_review = Review(actor, decision, message)
    proposal_reviews[actor] = new_review

    # Recompute tally
    proposal.tally = Tally()
    for rev in proposal_reviews.values():
        proposal.tally.by_actor[rev.actor] = rev.decision
        if rev.decision == 'approve':
            proposal.tally.approvals.add(rev.actor)
        elif rev.decision == 'reject':
            proposal.tally.rejections.add(rev.actor)

    # Recompute status
    proposal.status = evaluate_quorum(proposal)

    return success_response(proposal.to_dict())


@app.route('/v1/proposals/<int:proposal_id>:merge', methods=['POST'])
def merge_proposal(proposal_id: int):
    """Merge an approved proposal."""
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

    if proposal_id not in proposals:
        return error_response(
            'not_found',
            f'Proposal {proposal_id} not found',
            status=404
        )

    proposal = proposals[proposal_id]

    # Idempotent: if already merged, return same body
    if proposal.status == 'merged':
        # Find the activated version
        scope_fs = normalize_scope(proposal.scope)
        key = (proposal.name, scope_fs)
        activated_version = active_version_cache.get(key)
        return success_response({
            'activated_version': activated_version,
            'previous_active': proposal.base_version,
            'proposal_id': proposal_id
        })

    # Check if approved
    if proposal.status != 'approved':
        return error_response(
            'approval_required',
            f'Proposal {proposal_id} is not in approved status',
            status=409
        )

    # Check stale_base
    scope_fs = normalize_scope(proposal.scope)
    key = (proposal.name, scope_fs)
    current_active = active_version_cache.get(key)
    if current_active != proposal.base_version:
        return error_response(
            'stale_base',
            f'Proposal base_version {proposal.base_version} does not match current active version {current_active}',
            status=409
        )

    # Find the draft version
    draft_config = None
    if key in configs:
        for v in configs[key]:
            if v.version == proposal.draft_version:
                draft_config = v
                break

    if draft_config is None or draft_config.status != 'draft':
        return error_response(
            'not_mergeable',
            f'Draft version {proposal.draft_version} not found or not a draft',
            status=409
        )

    # Revalidate resolved config
    effective_schema = get_effective_schema(proposal.name, proposal.scope,
                                             draft_config.schema_ref)
    if effective_schema is not None:
        try:
            resolved_config, _ = resolve_config(proposal.name, proposal.scope,
                                                 proposal.draft_version)
            error_details = validate_against_schema(resolved_config, effective_schema)
            if error_details is not None:
                return error_response(
                    'not_mergeable',
                    'Resolved config does not conform to schema',
                    details=error_details,
                    status=409
                )
        except Exception as e:
            return error_response(
                'not_mergeable',
                f'Resolution failed: {str(e)}',
                status=409
            )

    # Activate the draft
    previous_active = current_active

    for v in configs[key]:
        if v.version == proposal.draft_version:
            v.active = True
            v.status = 'active'
        else:
            v.active = False
            if v.status == 'draft' or v.status == 'active':
                v.status = 'inactive'

    active_version_cache[key] = proposal.draft_version

    # Mark proposal as merged
    proposal.status = 'merged'

    # Supersede other open proposals for the same (name, scope)
    close_other_open_proposals(proposal.name, proposal.scope, proposal_id)

    return success_response({
        'activated_version': proposal.draft_version,
        'previous_active': previous_active,
        'proposal_id': proposal_id
    })


@app.route('/v1/proposals/<int:proposal_id>:withdraw', methods=['POST'])
def withdraw_proposal(proposal_id: int):
    """Withdraw a proposal (author only)."""
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
        actor = body.get('actor')
        validate_actor(actor)
        reason = body.get('reason')
        if reason is not None and not isinstance(reason, str):
            raise ValueError('reason must be a string')
    except ValueError as e:
        return error_response('invalid_input', str(e), status=400)

    if proposal_id not in proposals:
        return error_response(
            'not_found',
            f'Proposal {proposal_id} not found',
            status=404
        )

    proposal = proposals[proposal_id]

    # If already closed, return stored state
    if proposal.status in ('merged', 'withdrawn', 'superseded'):
        return success_response(proposal.to_dict())

    # Check actor is author
    if actor != proposal.author:
        return error_response(
            'policy_violation',
            'Only the proposal author may withdraw',
            status=422
        )

    proposal.status = 'withdrawn'

    return success_response(proposal.to_dict())


# -----------------------------------------------------------------------------
# Other Endpoints (from original, modified where needed)
# -----------------------------------------------------------------------------

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
        {'version': v.version, 'active': v.active, 'status': v.status}
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
                'status': v.status,
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
                'status': v.status,
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
    """Activate a specific version (requires approval)."""
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
        proposal_id = body.get('proposal_id')
        if not isinstance(proposal_id, int) or proposal_id < 1:
            raise ValueError('proposal_id must be a positive integer')
    except ValueError as e:
        return error_response('invalid_input', str(e), status=400)

    scope_fs = normalize_scope(scope)
    key = (name, scope_fs)

    # Check if version exists
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

    # Check proposal
    if proposal_id not in proposals:
        return error_response(
            'approval_required',
            f'Proposal {proposal_id} not found',
            status=409
        )

    proposal = proposals[proposal_id]

    if proposal.status != 'approved':
        return error_response(
            'approval_required',
            f'Proposal {proposal_id} is not approved',
            status=409
        )

    # Check stale_base
    current_active = active_version_cache.get(key)
    if current_active != proposal.base_version:
        return error_response(
            'stale_base',
            f'Proposal base_version {proposal.base_version} does not match current active version',
            status=409
        )

    # Check proposal draft matches activation target
    if proposal.draft_version != version:
        return error_response(
            'approval_required',
            f'Proposal draft version {proposal.draft_version} does not match target version {version}',
            status=409
        )

    # Merge the proposal (same as POST /v1/proposals/{id}:merge)
    return merge_proposal(proposal_id)


@app.route('/v1/configs/<name>:rollback', methods=['POST'])
def rollback(name: str):
    """Rollback to an earlier version (requires approval)."""
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
        to_version = body.get('to_version')
        if not isinstance(to_version, int) or to_version < 1:
            raise ValueError('to_version must be a positive integer')
        proposal_id = body.get('proposal_id')
        if not isinstance(proposal_id, int) or proposal_id < 1:
            raise ValueError('proposal_id must be a positive integer')
    except ValueError as e:
        return error_response('invalid_input', str(e), status=400)

    scope_fs = normalize_scope(scope)
    key = (name, scope_fs)

    # Get current active version
    current_active = active_version_cache.get(key)

    # Check if version exists
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

    if current_active is not None and to_version >= current_active and to_version != current_active:
        return error_response(
            'conflict',
            'to_version must be earlier than current active version',
            status=409
        )

    # Check proposal
    if proposal_id not in proposals:
        return error_response(
            'approval_required',
            f'Proposal {proposal_id} not found',
            status=409
        )

    proposal = proposals[proposal_id]

    if proposal.status != 'approved':
        return error_response(
            'approval_required',
            f'Proposal {proposal_id} is not approved',
            status=409
        )

    # Check stale_base
    if current_active != proposal.base_version:
        return error_response(
            'stale_base',
            f'Proposal base_version {proposal.base_version} does not match current active version',
            status=409
        )

    # Check proposal draft matches rollback target
    if proposal.draft_version != to_version:
        return error_response(
            'approval_required',
            f'Proposal draft version {proposal.draft_version} does not match target version {to_version}',
            status=409
        )

    # Merge the proposal
    return merge_proposal(proposal_id)


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


# =============================================================================
# Main Entry Point
# =============================================================================

def main():
    import argparse

    parser = argparse.ArgumentParser(description='Configuration Server with Change Management')
    parser.add_argument('--address', default='0.0.0.0',
                        help='Address to bind to')
    parser.add_argument('--port', type=int, default=8080,
                        help='Port to listen on')

    args = parser.parse_args()

    app.run(host=args.address, port=args.port, debug=False)


if __name__ == '__main__':
    main()
