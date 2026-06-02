#!/usr/bin/env python3
"""
Config Service - REST API for managing JSON configuration objects with
immutable versions, scoping, schema registry, binding, and validation.
"""

import asyncio
import hashlib
import json
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any
from urllib.parse import parse_qs

import yaml
import toml
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
import time
import uvicorn
# For JSON Schema validation
try:
    from jsonschema import Draft202012Validator, FormatChecker
    from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
    JSONSCHEMA_AVAILABLE = True
except ImportError:
    JSONSCHEMA_AVAILABLE = False


# =============================================================================
# Constants
# =============================================================================

MAX_REQUEST_SIZE = 1024 * 1024  # 1 MiB
MAX_INCLUDE_CHAIN = 64
MAX_VERSIONS_PER_SCOPE = 10_000
MAX_SCHEMA_VERSIONS_PER_NAME = 1000
MAX_RAW_DATA_SIZE = 1024 * 1024  # 1 MiB

# Workflow constants
MAX_PROPOSALS_PER_SCOPE = 1000
MAX_REVIEWS_PER_PROPOSAL = 1000
MAX_TITLE_BYTES = 200
MAX_DESCRIPTION_BYTES = 8 * 1024  # 8 KiB
MAX_LABELS = 32
MAX_LABEL_BYTES = 32
MAX_ACTOR_BYTES = 128


# =============================================================================
# Approval Policy Data Models
# =============================================================================

@dataclass
class ApprovalPolicy:
    """Policy for proposal approvals."""
    name: str
    scope: Scope
    required_approvals: int = 2
    allow_author_approval: bool = False
    allowed_reviewers: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            'name': self.name,
            'scope': self.scope,
            'required_approvals': self.required_approvals,
            'allow_author_approval': self.allow_author_approval,
            'allowed_reviewers': self.allowed_reviewers,
        }


class PolicyStorage:
    """In-memory storage for approval policies."""

    def __init__(self):
        # (name, scope_hash) -> ApprovalPolicy
        self._policies: dict[tuple[str, int], ApprovalPolicy] = {}

    def _key(self, name: str, scope: Scope) -> tuple[str, int]:
        return (name, scope_hash(scope))

    def get_policy(self, name: str, scope: Scope) -> ApprovalPolicy:
        """Get the policy for a (name, scope) pair."""
        key = self._key(name, scope)
        if key not in self._policies:
            # Return defaults
            return ApprovalPolicy(
                name=name,
                scope=scope,
                required_approvals=2,
                allow_author_approval=False,
                allowed_reviewers=None
            )
        return self._policies[key]

    def set_policy(self, name: str, scope: Scope, required_approvals: int,
                   allow_author_approval: bool,
                   allowed_reviewers: list[str] | None) -> ApprovalPolicy:
        """Set or update the policy for a (name, scope) pair."""
        # Validate required_approvals
        if not (1 <= required_approvals <= 10):
            raise ConfigError('policy_violation',
                            f'required_approvals must be in [1, 10], got {required_approvals}')

        # Validate actor strings if allowed_reviewers is provided
        if allowed_reviewers is not None:
            for actor in allowed_reviewers:
                if not isinstance(actor, str) or not actor:
                    raise ConfigError('policy_violation', 'allowed_reviewers must be non-empty strings')
                if len(actor) > MAX_ACTOR_BYTES:
                    raise ConfigError('policy_violation', f'actor exceeds {MAX_ACTOR_BYTES} bytes')

        policy = ApprovalPolicy(
            name=name,
            scope=scope,
            required_approvals=required_approvals,
            allow_author_approval=allow_author_approval,
            allowed_reviewers=allowed_reviewers
        )
        key = self._key(name, scope)
        self._policies[key] = policy
        return policy


# =============================================================================
# Proposal and Review Data Models
# =============================================================================

class ProposalStatus:
    """Proposal status values."""
    OPEN = 'open'
    APPROVED = 'approved'
    REJECTED = 'rejected'
    MERGED = 'merged'
    WITHDRAWN = 'withdrawn'
    SUPERSEDED = 'superseded'


@dataclass
class Tally:
    """Tally of reviews for a proposal."""
    approvals: int = 0
    rejections: int = 0
    by_actor: dict[str, str] = field(default_factory=dict)  # actor -> decision

    def to_dict(self) -> dict[str, Any]:
        return {
            'approvals': self.approvals,
            'rejections': self.rejections,
            'by_actor': dict(sorted(self.by_actor.items()))
        }


@dataclass
class DiffArtifacts:
    """Diff artifacts between draft and base versions."""
    raw_json_patch: list[dict[str, Any]]
    resolved_json_patch: list[dict[str, Any]]
    includes_changes: list[dict[str, Any]]
    human: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            'raw_json_patch': self.raw_json_patch,
            'resolved_json_patch': self.resolved_json_patch,
            'includes_changes': self.includes_changes,
            'human': self.human,
        }


@dataclass
class Proposal:
    """A proposal to merge a draft version."""
    proposal_id: int
    name: str
    scope: Scope
    draft_version: int
    base_version: int
    author: str
    title: str | None
    description: str | None
    labels: list[str]
    quorum: ApprovalPolicy  # Snapshot of policy at creation time
    status: str
    tally: Tally
    diffs: DiffArtifacts
    created_at: float  # timestamp

    def to_dict(self) -> dict[str, Any]:
        return {
            'proposal_id': self.proposal_id,
            'name': self.name,
            'scope': self.scope,
            'draft_version': self.draft_version,
            'base_version': self.base_version,
            'author': self.author,
            'title': self.title,
            'description': self.description,
            'labels': sorted(self.labels),
            'quorum': self.quorum.to_dict(),
            'status': self.status,
            'tally': self.tally.to_dict(),
            'diffs': self.diffs.to_dict(),
        }


class ReviewRecord:
    """Stores a review decision."""
    def __init__(self, actor: str, decision: str, message: str | None):
        self.actor = actor
        self.decision = decision  # 'approve' or 'reject'
        self.message = message


class ProposalStorage:
    """In-memory storage for proposals."""

    def __init__(self):
        # proposal_id -> Proposal
        self._proposals: dict[int, Proposal] = {}
        # Counter for generating proposal IDs
        self._next_proposal_id = 1
        # Reviews per proposal
        self._reviews: dict[int, dict[str, ReviewRecord]] = {}  # proposal_id -> actor -> ReviewRecord

    def create_proposal(self, proposal: Proposal) -> Proposal:
        """Create a new proposal."""
        proposal.proposal_id = self._next_proposal_id
        self._next_proposal_id += 1
        self._proposals[proposal.proposal_id] = proposal
        self._reviews[proposal.proposal_id] = {}
        return proposal

    def get_proposal(self, proposal_id: int) -> Proposal:
        """Get a proposal by ID."""
        if proposal_id not in self._proposals:
            raise ConfigError('not_found', f'Proposal {proposal_id} not found')
        return self._proposals[proposal_id]

    def update_proposal(self, proposal_id: int, proposal: Proposal) -> None:
        """Update a proposal."""
        self._proposals[proposal_id] = proposal

    def list_proposals(self, name: str, scope: Scope,
                       status_filter: str | None) -> list[Proposal]:
        """List proposals for a (name, scope) pair, optionally filtered by status."""
        proposals = [p for p in self._proposals.values()
                     if p.name == name and p.scope == scope]

        if status_filter is not None and status_filter != 'any':
            proposals = [p for p in proposals if p.status == status_filter]

        return sorted(proposals, key=lambda p: p.proposal_id)

    def add_review(self, proposal_id: int, review: ReviewRecord) -> None:
        """Add or update a review for a proposal."""
        if proposal_id not in self._reviews:
            raise ConfigError('not_found', f'Proposal {proposal_id} not found')

        reviews = self._reviews[proposal_id]
        if len(reviews) >= MAX_REVIEWS_PER_PROPOSAL:
            raise ConfigError('conflict', f'Maximum {MAX_REVIEWS_PER_PROPOSAL} reviews per proposal')

        reviews[review.actor] = review

    def get_reviews(self, proposal_id: int) -> dict[str, ReviewRecord]:
        """Get all reviews for a proposal."""
        if proposal_id not in self._reviews:
            raise ConfigError('not_found', f'Proposal {proposal_id} not found')
        return self._reviews[proposal_id]


# =============================================================================
# Diff Generation
# =============================================================================

def compute_drafts_storage_diff(base_config: dict[str, Any],
                                 draft_config: dict[str, Any]) -> list[dict[str, Any]]:
    """Compute RFC 6902 patch between two stored configs."""
    patch = []

    # Find removed and modified keys
    base_keys = set(base_config.keys())
    draft_keys = set(draft_config.keys())

    # Removed keys
    for key in sorted(base_keys - draft_keys):
        patch.append({'op': 'remove', 'path': f'/{key}'})

    # Modified or added keys
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
    # For nested objects, we need a more sophisticated approach
    def deep_diff(base, draft, path=''):
        diff = []

        if isinstance(base, dict) and isinstance(draft, dict):
            # Find removed keys
            for key in sorted(set(base.keys()) - set(draft.keys())):
                diff.append({'op': 'remove', 'path': f'{path}/{key}' if path else f'/{key}'})

            # Find added/modified keys
            for key in sorted(set(draft.keys()) - set(base.keys())):
                diff.append({'op': 'add', 'path': f'{path}/{key}' if path else f'/{key}', 'value': draft[key]})

            # Find modified values
            for key in sorted(set(base.keys()) & set(draft.keys())):
                if base[key] != draft[key]:
                    diff.extend(deep_diff(base[key], draft[key], f'{path}/{key}' if path else f'/{key}'))
        elif isinstance(base, list) and isinstance(draft, list):
            # For lists, treat as replacement
            if base != draft:
                diff.append({'op': 'replace', 'path': path, 'value': draft})
        elif base != draft:
            # Primitive value changed
            diff.append({'op': 'replace', 'path': path, 'value': draft})

        return diff

    patch = deep_diff(base_resolved, draft_resolved)

    # Sort lexicographically by path, then by operation type (remove, replace, add)
    op_order = {'remove': 0, 'replace': 1, 'add': 2}
    patch.sort(key=lambda x: (x['path'], op_order.get(x['op'], 3)))

    return patch


def compute_includes_changes(base_includes: tuple[IncludeRef, ...],
                              draft_includes: tuple[IncludeRef, ...]) -> list[dict[str, Any]]:
    """Compute changes to the includes list."""
    changes = []

    # Convert to dict for easier comparison
    base_dict = {i: ref for i, ref in enumerate(base_includes)}
    draft_dict = {i: ref for i, ref in enumerate(draft_includes)}

    # Find removed includes
    for idx in sorted(set(base_dict.keys()) - set(draft_dict.keys())):
        changes.append({
            'op': 'remove',
            'index': idx,
            'ref': base_dict[idx].to_dict()
        })

    # Find added includes
    for idx in sorted(set(draft_dict.keys()) - set(base_dict.keys())):
        changes.append({
            'op': 'add',
            'index': idx,
            'ref': draft_dict[idx].to_dict()
        })

    # Find updated includes (version pin changes)
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


def format_human_diff(diffs: DiffArtifacts,
                       base_config: dict[str, Any],
                       draft_config: dict[str, Any],
                       base_includes: tuple[IncludeRef, ...],
                       draft_includes: tuple[IncludeRef, ...]) -> list[str]:
    """Generate human-readable diff lines."""
    human = []

    # Process raw JSON patch for human diff
    for op in diffs.raw_json_patch:
        op_type = op['op']
        path = op['path']

        if op_type == 'remove':
            human.append(f'DELETE {path}')
        elif op_type == 'replace':
            old_val = get_nested_value(base_config, path)
            new_val = op.get('value')
            human.append(f'REPLACE {path}: {to_canonical_json(old_val).strip()} -> {to_canonical_json(new_val).strip()}')
        elif op_type == 'add':
            value = op.get('value')
            human.append(f'SET {path}: {to_canonical_json(value).strip()}')

    # Process includes changes
    for change in diffs.includes_changes:
        op = change['op']
        idx = change['index']
        ref = change.get('ref', {})
        name = ref.get('name', '?')
        scope = ref.get('scope', {})
        scope_str = ','.join(f'{k}={v}' for k, v in sorted(scope.items()))
        version = ref.get('version')

        scope_json = to_canonical_json(scope).strip()
        version_str = str(version) if version is not None else 'null'

        if op == 'add':
            human.append(f'INCLUDE_ADD [{idx}] {name}@{scope_json} v={version_str}')
        elif op == 'remove':
            human.append(f'INCLUDE_REMOVE [{idx}] {name}@{scope_json} v={version_str}')
        elif op == 'update':
            from_v = change.get('from_version')
            to_v = change.get('to_version')
            human.append(f'INCLUDE_UPDATE [{idx}] name@{scope_json}: {from_v} -> {to_v}')

    # Sort: DELETE, REPLACE, SET, then INCLUDE_* alphabetically
    def sort_key(s):
        if s.startswith('DELETE'):
            return (0, s)
        elif s.startswith('REPLACE'):
            return (1, s)
        elif s.startswith('SET'):
            return (2, s)
        elif s.startswith('INCLUDE_'):
            return (3, s)
        return (4, s)

    human.sort(key=sort_key)
    return human


def get_nested_value(config: dict[str, Any], path: str) -> Any:
    """Get a value from a nested dict using a JSON Pointer path."""
    if path == '/':
        return config

    parts = path.strip('/').split('/')
    current = config

    for part in parts:
        # Decode URL-encoded characters
        part = part.replace('~1', '/').replace('~0', '~')
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None

    return current


def compute_diff_artifacts(base_version: ConfigVersion,
                            draft_version: ConfigVersion,
                            storage: ConfigStorage) -> DiffArtifacts:
    """Compute all diff artifacts between base and draft versions."""

    # Compute raw JSON patch
    raw_patch = compute_drafts_storage_diff(base_version.config, draft_version.config)

    # Resolve both configs for comparison
    base_resolved, _ = resolve_config(storage, base_version.name,
                                       base_version.scope, base_version.version)
    draft_resolved, _ = resolve_config(storage, draft_version.name,
                                        draft_version.scope, draft_version.version)

    # Compute resolved JSON patch
    resolved_patch = compute_drafts_resolved_diff(base_resolved, draft_resolved)

    # Compute includes changes
    includes_changes = compute_includes_changes(base_version.includes, draft_version.includes)

    # Create temporary diffs object for human formatting
    temp_diffs = DiffArtifacts(
        raw_json_patch=raw_patch,
        resolved_json_patch=resolved_patch,
        includes_changes=includes_changes,
        human=[]
    )

    # Generate human diff
    human = format_human_diff(temp_diffs, base_version.config, draft_version.config,
                              base_version.includes, draft_version.includes)

    return DiffArtifacts(
        raw_json_patch=raw_patch,
        resolved_json_patch=resolved_patch,
        includes_changes=includes_changes,
        human=human
    )


# =============================================================================
# Quorum Evaluation
# =============================================================================

def evaluate_quorum(proposal: Proposal) -> str:
    """Evaluate the status of a proposal based on its reviews and policy."""
    # Check for rejections first
    if proposal.tally.rejections > 0:
        return ProposalStatus.REJECTED

    # Check if approved
    if proposal.tally.approvals >= proposal.quorum.required_approvals:
        return ProposalStatus.APPROVED

    # Otherwise open
    return ProposalStatus.OPEN


def validate_review_against_policy(actor: str, decision: str,
                                    proposal: Proposal) -> None:
    """Validate that a review complies with the policy."""
    # Check allowed_reviewers
    if proposal.quorum.allowed_reviewers is not None:
        if actor not in proposal.quorum.allowed_reviewers:
            raise ConfigError('policy_violation',
                            f'Actor {actor} is not in allowed_reviewers')

    # Check author approval
    if decision == 'approve' and actor == proposal.author:
        if not proposal.quorum.allow_author_approval:
            raise ConfigError('policy_violation',
                            'Authors cannot approve their own proposals')


# =============================================================================
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


# =============================================================================
# Schema Data Models
# =============================================================================

@dataclass(frozen=True)
class SchemaVersion:
    """An immutable version of a JSON Schema."""
    name: str
    version: int
    schema: dict[str, Any]  # The parsed JSON Schema object
    raw_source: str | None = None  # Original raw string if provided

    def to_dict(self) -> dict[str, Any]:
        return {
            'name': self.name,
            'version': self.version,
            'schema': self.schema,
        }


class SchemaStorage:
    """In-memory storage for schemas."""

    def __init__(self):
        # schema_name -> list of SchemaVersion sorted by version
        self._schemas: dict[str, list[SchemaVersion]] = {}

    def create_version(self, name: str, schema: dict[str, Any], raw_source: str | None = None) -> SchemaVersion:
        """Create a new immutable schema version."""
        if name not in self._schemas:
            self._schemas[name] = []

        versions = self._schemas[name]

        # Check max versions
        if len(versions) >= MAX_SCHEMA_VERSIONS_PER_NAME:
            raise ConfigError('conflict', f'Maximum {MAX_SCHEMA_VERSIONS_PER_NAME} versions reached for schema {name}')

        # Determine next version number
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
        """Get a specific schema version."""
        if name not in self._schemas:
            raise ConfigError('not_found', f'Schema {name} not found')

        for sv in self._schemas[name]:
            if sv.version == version:
                return sv

        raise ConfigError('not_found', f'Schema version {version} not found for {name}')

    def list_versions(self, name: str) -> list[SchemaVersion]:
        """List all versions for a schema name."""
        if name not in self._schemas:
            raise ConfigError('not_found', f'Schema {name} not found')
        return list(self._schemas[name])

    def get_latest(self, name: str) -> SchemaVersion | None:
        """Get the latest schema version, or None if not found."""
        if name not in self._schemas or not self._schemas[name]:
            return None
        return self._schemas[name][-1]


# =============================================================================
# Binding Data Models
# =============================================================================

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
        # (name, scope_hash) -> Binding
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

    def get_effective_schema(self, name: str, scope: Scope, schema_storage: SchemaStorage,
                             override_schema_ref: dict[str, int] | None = None) -> tuple[SchemaVersion | None, dict[str, int] | None]:
        """Get the effective schema for a config identity.

        Returns a tuple of (schema_version, schema_ref_used).
        If override_schema_ref is provided, use that.
        Otherwise, use the active binding if any.
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


# =============================================================================
# Raw Config Parsing
# =============================================================================

def check_merge_keys(obj, path=''):
    """Check for YAML merge keys (<<) in parsed object."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            current_path = f'{path}/{key}' if path else f'/{key}'
            if key == '<<':
                raise ConfigError('unprocessable', 'YAML merge keys (<<) are not allowed',
                                {'reason': 'yaml_feature_not_allowed'})
            check_merge_keys(value, current_path)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            check_merge_keys(item, f'{path}/{i}')


def check_json_types(obj, path=''):
    """Check for non-JSON types in TOML-parsed object."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            check_json_types(value, f'{path}/{key}' if path else f'/{key}')
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            check_json_types(item, f'{path}/{i}')
    elif obj is None:
        return
    elif isinstance(obj, (str, int, float, bool)):
        return
    else:
        raise ConfigError('unprocessable',
                        f'Non-JSON type {type(obj).__name__} at {path} is not allowed in TOML configs. '
                        f'Represent as strings if needed.',
                        {'reason': 'non_json_type'})


def check_for_external_refs(obj, path=''):
    """Check for external $ref/$dynamicRef in schema."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            current_path = f'{path}/{key}' if path else f'/{key}'
            if key == '$ref' and isinstance(value, str):
                if value.startswith('http://') or value.startswith('https://'):
                    raise ConfigError('schema_invalid', 'External $ref not allowed',
                                    {'reason': 'external_ref_not_allowed'})
            elif key == '$dynamicRef' and isinstance(value, str):
                if value.startswith('http://') or value.startswith('https://'):
                    raise ConfigError('schema_invalid', 'External $dynamicRef not allowed',
                                    {'reason': 'external_ref_not_allowed'})
            check_for_external_refs(value, current_path)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            check_for_external_refs(item, f'{path}/{i}')


def validate_yaml_features(raw_string: str, result: dict[str, Any]) -> None:
    """Validate YAML-specific features are not used."""
    # Check for anchors/aliases in the raw string
    if re.search(r'&[a-zA-Z0-9_]+|\*[a-zA-Z0-9_]+', raw_string):
        raise ConfigError('unprocessable', 'YAML anchors/aliases are not allowed',
                        {'reason': 'yaml_feature_not_allowed'})

    # Check for custom tags
    if re.search(r'!![a-zA-Z0-9_-]+', raw_string):
        raise ConfigError('unprocessable', 'YAML custom tags are not allowed',
                        {'reason': 'yaml_feature_not_allowed'})

    check_merge_keys(result)


def validate_toml_types(result: dict[str, Any]) -> None:
    """Validate TOML-parsed result contains only JSON-serializable types."""
    check_json_types(result)


def parse_raw_config(raw_string: str, raw_format: str) -> dict[str, Any]:
    """Parse a raw config string in the specified format to a JSON object.

    Args:
        raw_string: The raw config string.
        raw_format: One of 'json', 'yaml', 'toml'.

    Returns:
        The parsed JSON object (must be a dict).

    Raises:
        ConfigError: On parse errors or invalid formats.
    """
    if len(raw_string) > MAX_RAW_DATA_SIZE:
        raise ConfigError('too_large', 'Raw config exceeds 1 MiB limit')

    raw_string = raw_string.strip()

    if raw_format == 'json':
        try:
            result = json.loads(raw_string)
        except json.JSONDecodeError as e:
            raise ConfigError('unprocessable', f'Invalid JSON: {e}', {'reason': 'invalid_json'})

    elif raw_format == 'yaml':
        try:
            result = yaml.safe_load(raw_string)
        except yaml.YAMLError as e:
            raise ConfigError('unprocessable', f'Invalid YAML: {e}', {'reason': 'invalid_yaml'})

        validate_yaml_features(raw_string, result)

    elif raw_format == 'toml':
        try:
            result = toml.loads(raw_string)
        except toml.TomlDecodeError as e:
            raise ConfigError('unprocessable', f'Invalid TOML: {e}', {'reason': 'invalid_toml'})

        validate_toml_types(result)

    else:
        raise ConfigError('unsupported_format', f'Unsupported raw format: {raw_format}',
                         {'supported_formats': ['json', 'yaml', 'toml']})

    # The root must be a JSON object (dict), not array or scalar
    if not isinstance(result, dict):
        raise ConfigError('unprocessable',
                         f'Config root must be a JSON object, got {type(result).__name__}',
                         {'reason': 'root_not_object'})

    return result


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
# JSON Schema Validation
# =============================================================================

def validate_schema_against_itself(schema_doc: dict[str, Any]) -> tuple[bool, str | None]:
    """Validate that a document is a valid JSON Schema Draft 2020-12.

    Returns (is_valid, error_message).
    """
    # Basic structural validation
    if not isinstance(schema_doc, dict):
        return False, 'Schema must be a JSON object'

    # Check for $schema declaration (optional but recommended)
    # Not strictly required, but we can accept any schema if it's structurally valid

    # Check for external $ref - not allowed
    if '$ref' in schema_doc:
        ref_value = schema_doc['$ref']
        if isinstance(ref_value, str):
            # Reject remote/HTTP refs
            if ref_value.startswith('http://') or ref_value.startswith('https://'):
                return False, 'External $ref not allowed'
            # In-document refs start with #/ or are just a path within the document
            # We allow those, the validator will resolve them

    # Check that all keywords are valid (simplified check)
    # We'll rely on the jsonschema validator for more thorough checks

    # Check for disallowed features in schema itself
    if 'allOf' in schema_doc or 'anyOf' in schema_doc or 'oneOf' in schema_doc:
        # These are allowed, but $dynamicRef/$dynamicAnchor are not in 2020-12?
        # Actually they are in 2020-12. We should check for $dynamicRef specifically
        pass

    if '$dynamicRef' in schema_doc:
        # Dynamic refs are part of 2020-12 but we might want to restrict them
        # or treat them like regular refs. For simplicity, allow them but
        # they must also be in-document
        ref_value = schema_doc.get('$dynamicRef', '')
        if isinstance(ref_value, str) and (ref_value.startswith('http://') or ref_value.startswith('https://')):
            return False, 'External $dynamicRef not allowed'

    return True, None


def validate_against_schema(instance: dict[str, Any], schema_doc: dict[str, Any]) -> tuple[bool, dict[str, str] | None]:
    """Validate an instance against a JSON Schema.

    Returns (is_valid, error_details) where error_details contains:
      - path: JSON Pointer to the failing location
      - rule: the violated rule keyword
      - expected: what was expected
      - actual: what was found
    """
    try:
        # Create a validator for Draft 2020-12
        validator = Draft202012Validator(schema_doc)

        # Collect all errors
        errors = list(validator.iter_errors(instance))

        if not errors:
            return True, None

        # Find the lexicographically smallest JSON Pointer path among failures
        best_error = None
        best_path = None

        for error in errors:
            # Get the JSON Pointer path
            path = error.json_path

            # Determine the primary violated keyword
            rule = error.validator

            # Build details based on the rule
            details = {
                'path': path if path else '/',
                'rule': rule,
            }

            # Add expected/actual based on rule type
            if rule == 'type':
                expected = error.validator_value
                if isinstance(expected, list):
                    expected = expected[0] if expected else 'unknown'
                details['expected'] = str(expected)
                # Get actual type from the instance
                actual_value = error.instance
                details['actual'] = type(actual_value).__name__
            elif rule == 'enum':
                details['expected'] = 'one of ' + str(error.validator_value)
                details['actual'] = str(error.instance)
            elif rule == 'required':
                details['expected'] = 'required property ' + str(error.validator_value)
                details['actual'] = 'missing'
            elif rule == 'pattern':
                details['expected'] = f'pattern {error.validator_value}'
                details['actual'] = str(error.instance)[:50] + ('...' if len(str(error.instance)) > 50 else '')
            elif rule == 'minimum':
                details['expected'] = f'minimum {error.validator_value}'
                details['actual'] = str(error.instance)
            elif rule == 'maximum':
                details['expected'] = f'maximum {error.validator_value}'
                details['actual'] = str(error.instance)
            elif rule == 'minLength':
                details['expected'] = f'minimum length {error.validator_value}'
                details['actual'] = f'length {len(str(error.instance))}'
            elif rule == 'maxLength':
                details['expected'] = f'maximum length {error.validator_value}'
                details['actual'] = f'length {len(str(error.instance))}'
            elif rule == 'minProperties':
                details['expected'] = f'minimum {error.validator_value} properties'
                details['actual'] = f'{len(error.instance)} properties'
            elif rule == 'maxProperties':
                details['expected'] = f'maximum {error.validator_value} properties'
                details['actual'] = f'{len(error.instance)} properties'
            elif rule == 'multipleOf':
                details['expected'] = f'multiple of {error.validator_value}'
                details['actual'] = str(error.instance)
            else:
                details['expected'] = str(error.validator_value)[:100]
                details['actual'] = str(error.instance)[:100]

            # Select lexicographically smallest path
            if best_path is None or path < best_path:
                best_path = path
                best_error = details

        return False, best_error

    except Exception as e:
        # Schema itself is invalid
        return False, {
            'path': '/',
            'rule': 'schema_error',
            'expected': 'valid JSON Schema',
            'actual': str(e)
        }


# =============================================================================
# Existing Data Models (from Part 1, kept for compatibility)
# =============================================================================

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
    status: str = 'draft'  # 'draft' or 'active'

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


# =============================================================================
# Storage (from Part 1, extended for bindings)
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
            active=False,
            status='draft'
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

app = FastAPI(title='Config Service', version='2.0.0')
storage = ConfigStorage()
schema_storage = SchemaStorage()
binding_storage = BindingStorage()
policy_storage = PolicyStorage()
proposal_storage = ProposalStorage()


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


class CreateConfigRequestExtended(BaseModel):
    """Extended request for creating a config with raw config or schema override."""

    scope: dict[str, str]
    config: dict[str, Any] | None = None
    raw_config: str | None = None
    raw_format: str | None = None
    includes: list[dict[str, Any]] = []
    schema_ref: dict[str, int] | None = None
    inherits_active: bool = False

    @field_validator('raw_format')
    @classmethod
    def validate_raw_format(cls, v):
        if v not in ('json', 'yaml', 'toml'):
            raise ValueError('raw_format must be one of: json, yaml, toml')
        return v

    @field_validator('schema_ref')
    @classmethod
    def validate_schema_ref(cls, v):
        if v is not None:
            if 'name' not in v or 'version' not in v:
                raise ValueError('schema_ref must have "name" and "version"')
            if not isinstance(v['name'], str) or not isinstance(v['version'], int):
                raise ValueError('schema_ref.name must be string and schema_ref.version must be integer')
            if v['version'] < 1:
                raise ValueError('schema_ref.version must be a positive integer')
        return v


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
    schema_ref: dict[str, int] | None = None
    dry_run: bool = False


class ResolveResponse(BaseModel):
    """Response for resolving a config."""

    name: str
    scope: dict[str, str]
    version_used: int
    resolved_config: dict[str, Any]
    resolution_graph: list[dict[str, Any]]
    validated_against: dict[str, int] | None = None


class ValidateRequest(BaseModel):
    """Request for validating a config."""

    scope: dict[str, str]
    version: int | None = None
    schema_ref: dict[str, int] | None = None
    mode: str = 'resolved'


class ValidateResponse(BaseModel):
    """Response for validation."""

    name: str
    scope: dict[str, str]
    version_used: int
    mode: str
    valid: bool
    validated_against: dict[str, int] | None = None


class RollbackRequest(BaseModel):
    """Request for rollback."""

    scope: dict[str, str]
    to_version: int


class HealthResponse(BaseModel):
    """Health check response."""

    ok: bool


# =============================================================================
# Schema-related request/response models
# =============================================================================

class CreateSchemaRequest(BaseModel):
    """Request for creating a schema."""

    schema: dict[str, Any] | None = None
    raw_schema: str | None = None
    raw_format: str | None = None

    @field_validator('raw_format')
    @classmethod
    def validate_raw_format(cls, v):
        if v not in ('json', 'yaml'):
            raise ValueError('raw_format must be one of: json, yaml')
        return v


class SchemaVersionResponse(BaseModel):
    """Response for schema version creation."""

    name: str
    version: int


class SchemaListResponse(BaseModel):
    """Response for listing schema versions."""

    name: str
    versions: list[dict[str, int]]


class SchemaDetailResponse(BaseModel):
    """Response for getting a schema."""

    name: str
    version: int
    schema: dict[str, Any]


class BindRequest(BaseModel):
    """Request for binding a schema to a config identity."""

    scope: dict[str, str]
    schema_ref: dict[str, int]

    @field_validator('schema_ref')
    @classmethod
    def validate_schema_ref(cls, v):
        if 'name' not in v or 'version' not in v:
            raise ValueError('schema_ref must have "name" and "version"')
        if not isinstance(v['name'], str) or not isinstance(v['version'], int):
            raise ValueError('schema_ref.name must be string and schema_ref.version must be integer')
        if v['version'] < 1:
            raise ValueError('schema_ref.version must be a positive integer')
        return v


class BindResponse(BaseModel):
    """Response for binding."""

    name: str
    scope: dict[str, str]
    schema_ref: dict[str, int]
    active: bool


class SchemaRefResponse(BaseModel):
    """Response for reading the active binding."""

    name: str
    scope: dict[str, str]
    schema_ref: dict[str, int]


# =============================================================================
# Request helpers
# =============================================================================

def _parse_body(raw: bytes) -> dict[str, Any]:
    """Parse request body to dict."""
    if not raw:
        raise ConfigError('invalid_input', 'Request body is required')
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ConfigError('invalid_input', f'Invalid JSON: {e}')


async def _read_body(request: Request) -> dict[str, Any]:
    """Read and parse request body."""
    body = await request.body()
    return _parse_body(body)


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
        'approval_required': 409,
        'stale_base': 409,
        'not_mergeable': 409,
        'unprocessable': 422,
        'rate_limited': 429,
        'too_large': 413,
        'unsupported_format': 415,
        'schema_invalid': 422,
        'validation_failed': 422,
        'policy_violation': 422,
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
    return error_response('internal', str(exc.detail), {'status_code': exc.status_code})


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
# Endpoints (Part 1 - kept for compatibility)
# =============================================================================

@app.get('/healthz')
async def healthcheck() -> HealthResponse:
    """Health check endpoint."""
    return HealthResponse(ok=True)


@app.post('/v1/configs/{name}')
async def create_config(request: Request, name: str):
    """Create a new version of a config."""
    body_data = await request.body()
    if not body_data:
        raise ConfigError('invalid_input', 'Request body is required')

    try:
        data = json.loads(body_data)
    except json.JSONDecodeError as e:
        raise ConfigError('invalid_input', f'Invalid JSON: {e}')

    # Check if using extended format (raw_config or schema_ref)
    use_extended = 'raw_config' in data or 'schema_ref' in data

    if use_extended:
        body = CreateConfigRequestExtended.model_validate(data)
        scope = body.scope
        includes = [IncludeRef.from_dict(inc.model_dump()) for inc in body.includes]
        inherits_active = body.inherits_active
        schema_ref_override = body.schema_ref

        # Parse config content
        if body.config is not None:
            config = body.config
        else:
            raw_config = body.raw_config
            raw_format = body.raw_format or 'json'
            if not isinstance(raw_config, str):
                raise ConfigError('invalid_input', 'raw_config must be a string')
            config = parse_raw_config(raw_config, raw_format)
    else:
        body = CreateConfigRequest.model_validate(data)
        scope = body.scope
        config = body.config
        includes = [IncludeRef.from_dict(inc) for inc in body.includes]
        inherits_active = body.inherits_active
        schema_ref_override = None

    # Validate config is a dict
    if not isinstance(config, dict):
        raise ConfigError('invalid_input', 'Config must be a JSON object')

    # Determine effective schema
    schema_version, _ = binding_storage.get_effective_schema(name, scope, schema_storage, schema_ref_override)

    # Validate against schema if one exists
    if schema_version is not None:
        is_valid, error_details = validate_against_schema(config, schema_version.schema)
        if not is_valid:
            raise ConfigError('validation_failed', 'Config does not conform to schema', error_details)

    new_version = storage.create_version(name, scope, config, includes, inherits_active)

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
    data = await _read_body(request)

    if 'scope' not in data:
        raise ConfigError('invalid_input', 'Missing required field: scope')

    scope = validate_scope(data['scope'])
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
    data = await _read_body(request)

    if 'scope' not in data:
        raise ConfigError('invalid_input', 'Missing required field: scope')

    config_version = storage.get_version(name, validate_scope(data['scope']), version)

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
    data = await _read_body(request)

    if 'scope' not in data:
        raise ConfigError('invalid_input', 'Missing required field: scope')

    config_version = storage.get_active(name, validate_scope(data['scope']))

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
    data = await _read_body(request)

    if 'scope' not in data:
        raise ConfigError('invalid_input', 'Missing required field: scope')

    activated = storage.activate_version(name, validate_scope(data['scope']), version)

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
    data = await _read_body(request)

    if 'scope' not in data:
        raise ConfigError('invalid_input', 'Missing required field: scope')
    if 'to_version' not in data:
        raise ConfigError('invalid_input', 'Missing required field: to_version')

    to_version = data['to_version']
    if not isinstance(to_version, int) or to_version < 1:
        raise ConfigError('invalid_input', 'to_version must be a positive integer')

    rolled_back = storage.rollback(name, validate_scope(data['scope']), to_version)

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
    """Resolve a config with all imports applied, with optional validation."""
    data = await _read_body(request)

    if 'scope' not in data:
        raise ConfigError('invalid_input', 'Missing required field: scope')

    body = ResolveRequest.model_validate(data)
    scope = body.scope
    version = body.version
    schema_ref_override = body.schema_ref

    # Perform resolution
    resolved_config, resolution_graph = resolve_config(storage, name, scope, version)

    # Determine version_used
    if version is None:
        config_version = storage.get_active(name, scope)
        version_used = config_version.version
    else:
        version_used = version

    # Determine effective schema for validation
    schema_version, schema_ref = binding_storage.get_effective_schema(
        name, scope, schema_storage, schema_ref_override
    )

    validated_against = None
    if schema_version is not None:
        is_valid, error_details = validate_against_schema(resolved_config, schema_version.schema)
        if not is_valid:
            raise ConfigError('validation_failed', 'Config does not conform to schema', error_details)
        validated_against = schema_ref

    response = {
        'name': name,
        'scope': scope,
        'version_used': version_used,
        'resolved_config': resolved_config,
        'resolution_graph': [node.to_dict() for node in resolution_graph],
    }
    if validated_against is not None:
        response['validated_against'] = validated_against

    return JSONResponse(
        status_code=200,
        content=to_canonical_json(response),
        media_type='application/json; charset=utf-8'
    )


# =============================================================================
# NEW: Schema endpoints
# =============================================================================

@app.post('/v1/schemas/{schema_name}')
async def create_schema(request: Request, schema_name: str):
    """Create a new version of a JSON Schema."""
    body_data = await parse_body(request)

    schema_doc = None

    # Check for structured JSON schema (preferred)
    if 'schema' in body_data:
        schema_doc = body_data['schema']
        if not isinstance(schema_doc, dict):
            raise ConfigError('invalid_input', 'Schema must be a JSON object')
        raw_source = None

    # Or raw schema
    elif 'raw_schema' in body_data:
        raw_schema = body_data['raw_schema']
        raw_format = body_data.get('raw_format', 'json')

        if not isinstance(raw_schema, str):
            raise ConfigError('invalid_input', 'raw_schema must be a string')

        if raw_format == 'json':
            try:
                schema_doc = json.loads(raw_schema)
            except json.JSONDecodeError as e:
                raise ConfigError('schema_invalid', f'Invalid JSON schema: {e}', {'reason': 'invalid_json'})
        elif raw_format == 'yaml':
            try:
                schema_doc = yaml.safe_load(raw_schema)
            except yaml.YAMLError as e:
                raise ConfigError('schema_invalid', f'Invalid YAML schema: {e}', {'reason': 'invalid_yaml'})
        else:
            raise ConfigError('unsupported_format', f'Unsupported raw format: {raw_format}')

        raw_source = raw_schema

    else:
        raise ConfigError('invalid_input', 'Must provide either "schema" or "raw_schema"')

    # Validate it's a valid JSON Schema 2020-12
    is_valid, error_msg = validate_schema_against_itself(schema_doc)
    if not is_valid:
        raise ConfigError('schema_invalid', error_msg, {'reason': 'invalid_schema'})

    # Additional check: no external $ref
    try:
        check_for_external_refs(schema_doc)
    except ConfigError:
        raise

    # Create schema version
    new_schema = schema_storage.create_version(schema_name, schema_doc, raw_source)

    return JSONResponse(
        status_code=201,
        content=to_canonical_json({
            'name': new_schema.name,
            'version': new_schema.version,
        }),
        media_type='application/json; charset=utf-8'
    )


@app.post('/v1/schemas/{schema_name}/versions')
async def list_schema_versions(request: Request, schema_name: str):
    """List all versions of a schema."""
    body_data = await parse_body(request)
    # Body can be empty or {}

    versions = schema_storage.list_versions(schema_name)
    versions.sort(key=lambda v: v.version)

    return JSONResponse(
        status_code=200,
        content=to_canonical_json({
            'name': schema_name,
            'versions': [{'version': v.version} for v in versions]
        }),
        media_type='application/json; charset=utf-8'
    )


@app.post('/v1/schemas/{schema_name}/{schema_version}')
async def get_schema(request: Request, schema_name: str, schema_version: int):
    """Get a specific schema version."""
    body_data = await parse_body(request)
    # Body can be empty or {}

    schema_ver = schema_storage.get_version(schema_name, schema_version)

    return JSONResponse(
        status_code=200,
        content=to_canonical_json({
            'name': schema_ver.name,
            'version': schema_ver.version,
            'schema': schema_ver.schema,
        }),
        media_type='application/json; charset=utf-8'
    )


# =============================================================================
# NEW: Binding endpoints
# =============================================================================

@app.post('/v1/configs/{name}:bind')
async def bind_schema(request: Request, name: str):
    """Bind a schema to a config identity."""
    body_data = await parse_body(request)

    if 'scope' not in body_data:
        raise ConfigError('invalid_input', 'Missing required field: scope')
    if 'schema_ref' not in body_data:
        raise ConfigError('invalid_input', 'Missing required field: schema_ref')

    scope = validate_scope(body_data['scope'])
    schema_ref = body_data['schema_ref']

    if not isinstance(schema_ref, dict):
        raise ConfigError('invalid_input', 'schema_ref must be an object')
    if 'name' not in schema_ref or 'version' not in schema_ref:
        raise ConfigError('invalid_input', 'schema_ref must have "name" and "version"')

    schema_name = schema_ref['name']
    schema_version_num = schema_ref['version']

    if not isinstance(schema_name, str):
        raise ConfigError('invalid_input', 'schema_ref.name must be a string')
    if not isinstance(schema_version_num, int) or schema_version_num < 1:
        raise ConfigError('invalid_input', 'schema_ref.version must be a positive integer')

    # Check that the schema exists
    try:
        schema_storage.get_version(schema_name, schema_version_num)
    except ConfigError as e:
        if e.code == 'not_found':
            raise ConfigError('conflict', f'Schema {schema_name} version {schema_version_num} does not exist')
        raise

    # Create or update binding
    binding = binding_storage.bind(name, scope, schema_name, schema_version_num)

    return JSONResponse(
        status_code=200,
        content=to_canonical_json({
            'name': binding.name,
            'scope': binding.scope,
            'schema_ref': {
                'name': binding.schema_name,
                'version': binding.schema_version,
            },
            'active': binding.active,
        }),
        media_type='application/json; charset=utf-8'
    )


@app.post('/v1/configs/{name}/schema')
async def get_binding(request: Request, name: str):
    """Read the active binding for a config identity."""
    body_data = await parse_body(request)

    if 'scope' not in body_data:
        raise ConfigError('invalid_input', 'Missing required field: scope')

    scope = validate_scope(body_data['scope'])

    binding = binding_storage.get_binding(name, scope)

    if binding is None:
        raise ConfigError('not_found', f'No binding found for {name} with scope {scope}')

    return JSONResponse(
        status_code=200,
        content=to_canonical_json({
            'name': binding.name,
            'scope': binding.scope,
            'schema_ref': {
                'name': binding.schema_name,
                'version': binding.schema_version,
            },
        }),
        media_type='application/json; charset=utf-8'
    )


# =============================================================================
# NEW: Validate-only endpoint
# =============================================================================

@app.post('/v1/configs/{name}:validate')
async def validate_config(request: Request, name: str):
    """Validate a config version or resolved config against a schema."""
    body_data = await parse_body(request)

    if 'scope' not in body_data:
        raise ConfigError('invalid_input', 'Missing required field: scope')

    scope = validate_scope(body_data['scope'])
    version = body_data.get('version')
    schema_ref_override = body_data.get('schema_ref')
    mode = body_data.get('mode', 'resolved')

    if mode not in ('stored', 'resolved'):
        raise ConfigError('invalid_input', 'mode must be "stored" or "resolved"')

    if version is not None:
        if not isinstance(version, int) or version < 1:
            raise ConfigError('invalid_input', 'Version must be a positive integer')

    # Determine which config to validate
    if mode == 'stored':
        # Get the stored config
        try:
            if version is None:
                config_version = storage.get_active(name, scope)
            else:
                config_version = storage.get_version(name, scope, version)

            config_to_validate = config_version.config
            version_used = config_version.version
        except ConfigError as e:
            if e.code == 'not_found':
                raise ConfigError('not_found', f'Config version not found for {name}')
            raise
    else:
        # mode == 'resolved': resolve and validate the resolved config
        try:
            resolved_config, _ = resolve_config(storage, name, scope, version)
            # Determine version_used
            if version is None:
                config_version = storage.get_active(name, scope)
                version_used = config_version.version
            else:
                version_used = version
            config_to_validate = resolved_config
        except ConfigError as e:
            if e.code == 'not_found':
                raise ConfigError('not_found', f'Config version not found for {name}')
            raise

    # Determine effective schema
    schema_version, schema_ref = binding_storage.get_effective_schema(
        name, scope, schema_storage, schema_ref_override
    )

    if schema_version is None:
        # No schema - still return valid but with null validated_against
        return JSONResponse(
            status_code=200,
            content=to_canonical_json({
                'name': name,
                'scope': scope,
                'version_used': version_used,
                'mode': mode,
                'valid': True,
                'validated_against': None,
            }),
            media_type='application/json; charset=utf-8'
        )

    # Validate
    is_valid, error_details = validate_against_schema(config_to_validate, schema_version.schema)

    if not is_valid:
        raise ConfigError('validation_failed', 'Config does not conform to schema', error_details)

    return JSONResponse(
        status_code=200,
        content=to_canonical_json({
            'name': name,
            'scope': scope,
            'version_used': version_used,
            'mode': mode,
            'valid': True,
            'validated_against': schema_ref,
        }),
        media_type='application/json; charset=utf-8'
    )


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
