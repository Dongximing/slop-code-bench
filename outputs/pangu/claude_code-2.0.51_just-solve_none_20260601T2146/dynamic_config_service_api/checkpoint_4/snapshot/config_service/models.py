#!/usr/bin/env python3
"""Data models for the Config Service."""

from dataclasses import dataclass, field
from typing import Any
from functools import lru_cache


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
    """Validate scope dict."""
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
        self._policies: dict[tuple[str, int], ApprovalPolicy] = {}

    def _key(self, name: str, scope: Scope) -> tuple[str, int]:
        return (name, scope_hash(scope))

    def get_policy(self, name: str, scope: Scope) -> ApprovalPolicy:
        key = self._key(name, scope)
        if key not in self._policies:
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
        if not (1 <= required_approvals <= 10):
            raise ConfigError('policy_violation',
                            f'required_approvals must be in [1, 10], got {required_approvals}')

        if allowed_reviewers is not None:
            for actor in allowed_reviewers:
                if not isinstance(actor, str) or not actor:
                    raise ConfigError('policy_violation', 'allowed_reviewers must be non-empty strings')
                if len(actor) > 128:
                    raise ConfigError('policy_violation', f'actor exceeds 128 bytes')

        policy = ApprovalPolicy(
            name=name,
            scope=scope,
            required_approvals=required_approvals,
            allow_author_approval=allow_author_approval,
            allowed_reviewers=allowed_reviewers
        )
        self._policies[self._key(name, scope)] = policy
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
    by_actor: dict[str, str] = field(default_factory=dict)

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
    quorum: ApprovalPolicy
    status: str
    tally: Tally
    diffs: DiffArtifacts
    created_at: float

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
        self.decision = decision
        self.message = message


class ProposalStorage:
    """In-memory storage for proposals."""

    def __init__(self):
        self._proposals: dict[int, Proposal] = {}
        self._next_proposal_id = 1
        self._reviews: dict[int, dict[str, ReviewRecord]] = {}

    def create_proposal(self, proposal: Proposal) -> Proposal:
        proposal.proposal_id = self._next_proposal_id
        self._next_proposal_id += 1
        self._proposals[proposal.proposal_id] = proposal
        self._reviews[proposal.proposal_id] = {}
        return proposal

    def get_proposal(self, proposal_id: int) -> Proposal:
        if proposal_id not in self._proposals:
            raise ConfigError('not_found', f'Proposal {proposal_id} not found')
        return self._proposals[proposal_id]

    def update_proposal(self, proposal_id: int, proposal: Proposal) -> None:
        self._proposals[proposal_id] = proposal

    def list_proposals(self, name: str, scope: Scope,
                       status_filter: str | None) -> list[Proposal]:
        proposals = [p for p in self._proposals.values()
                     if p.name == name and p.scope == scope]

        if status_filter is not None and status_filter != 'any':
            proposals = [p for p in proposals if p.status == status_filter]

        return sorted(proposals, key=lambda p: p.proposal_id)

    def add_review(self, proposal_id: int, review: ReviewRecord) -> None:
        if proposal_id not in self._reviews:
            raise ConfigError('not_found', f'Proposal {proposal_id} not found')

        reviews = self._reviews[proposal_id]
        if len(reviews) >= 1000:
            raise ConfigError('conflict', f'Maximum 1000 reviews per proposal')

        reviews[review.actor] = review

    def get_reviews(self, proposal_id: int) -> dict[str, ReviewRecord]:
        if proposal_id not in self._reviews:
            raise ConfigError('not_found', f'Proposal {proposal_id} not found')
        return self._reviews[proposal_id]


# =============================================================================
# Quorum Evaluation
# =============================================================================

def evaluate_quorum(proposal: Proposal) -> str:
    """Evaluate the status of a proposal based on its reviews and policy."""
    if proposal.tally.rejections > 0:
        return ProposalStatus.REJECTED
    if proposal.tally.approvals >= proposal.quorum.required_approvals:
        return ProposalStatus.APPROVED
    return ProposalStatus.OPEN


def validate_review_against_policy(actor: str, decision: str,
                                   proposal: Proposal) -> None:
    """Validate that a review complies with the policy."""
    if proposal.quorum.allowed_reviewers is not None:
        if actor not in proposal.quorum.allowed_reviewers:
            raise ConfigError('policy_violation',
                            f'Actor {actor} is not in allowed_reviewers')

    if decision == 'approve' and actor == proposal.author:
        if not proposal.quorum.allow_author_approval:
            raise ConfigError('policy_violation',
                            'Authors cannot approve their own proposals')


# =============================================================================
# Policy Bundle Data Models
# =============================================================================

@dataclass
class PolicyBundle:
    """A versioned package of policy modules and optional data."""
    name: str
    version: int
    rego_modules: dict[str, str]
    data: dict[str, Any] | None = None
    metadata: dict[str, str] | None = None
    created_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {
            'bundle_name': self.name,
            'version': self.version,
        }
        if self.metadata:
            result['metadata'] = self.metadata
        return result

    def summary_dict(self) -> dict[str, Any]:
        """Return a summary for listing versions."""
        result = {
            'version': self.version,
        }
        if self.metadata:
            result['metadata'] = self.metadata
        return result


# =============================================================================
# Policy Binding Data Models
# =============================================================================

@dataclass
class PolicyBinding:
    """Associates a bundle version with a selector and defines graph behavior."""
    binding_id: int
    bundle: dict[str, int]  # {"name": str, "version": int}
    selector: dict[str, str]
    graph_keys: list[str]
    priority: int

    def to_dict(self) -> dict[str, Any]:
        return {
            'binding_id': self.binding_id,
            'bundle': self.bundle,
            'selector': self.selector,
            'graph_keys': self.graph_keys,
            'priority': self.priority,
        }


# =============================================================================
# Policy Evaluation Result Data Models
# =============================================================================

@dataclass
class PolicyStackEntry:
    """An entry in the policy stack."""
    bundle: dict[str, int]  # {"name": str, "version": int}
    selector: dict[str, str]
    graph_keys: list[str]
    priority: int

    def to_dict(self) -> dict[str, Any]:
        return {
            'bundle': self.bundle,
            'selector': self.selector,
            'graph_keys': self.graph_keys,
            'priority': self.priority,
        }


@dataclass
class PolicyViolation:
    """A single policy violation."""
    policy: dict[str, int]  # {"name": str, "version": int}
    target: dict[str, Any]  # {"name": str, "scope": dict, "version_used": int}
    rule_id: str
    severity: str  # "error" or "warn"
    path: str
    message: str
    evidence: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {
            'policy': self.policy,
            'target': self.target,
            'rule_id': self.rule_id,
            'severity': self.severity,
            'path': self.path,
            'message': self.message,
        }
        if self.evidence is not None:
            result['evidence'] = self.evidence
        return result


@dataclass
class PolicyEvaluationResult:
    """The canonical result of a policy evaluation."""
    policy_stack: list[PolicyStackEntry]
    violations: list[PolicyViolation]
    truncated: bool = False
    graph_truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        tally = {'errors': 0, 'warnings': 0}
        for v in self.violations:
            if v.severity == 'error':
                tally['errors'] += 1
            elif v.severity == 'warn':
                tally['warnings'] += 1

        result = {
            'policy_stack': [e.to_dict() for e in self.policy_stack],
            'violations': sorted(
                [v.to_dict() for v in self.violations],
                key=lambda v: (
                    v.get('target', {}).get('name', ''),
                    v.get('policy', {}).get('name', ''),
                    v.get('policy', {}).get('version', 0),
                    v.get('rule_id', ''),
                    v.get('path', '')
                )
            ),
            'tally': tally,
        }
        if self.truncated:
            result['truncated'] = True
        if self.graph_truncated:
            result['details'] = {'graph_truncated': True}
        return result


# =============================================================================
# Policy Summary for Proposals
# =============================================================================

@dataclass
class PolicySummary:
    """Policy evaluation summary stored on proposals."""
    evaluation: PolicyEvaluationResult
    evaluated_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            'evaluation': self.evaluation.to_dict(),
            'evaluated_at': self.evaluated_at,
        }


# =============================================================================
# Policy Storage (for bundles and bindings)
# =============================================================================

MAX_POLICY_BUNDLES = 500
MAX_VERSIONS_PER_BUNDLE = 200
MAX_POLICY_BINDINGS = 5000
MAX_REGO_PAYLOAD_SIZE = 1024 * 1024  # 1 MiB


class PolicyBundleStorage:
    """In-memory storage for policy bundles."""

    def __init__(self):
        # bundle_name -> list of PolicyBundle sorted by version
        self._bundles: dict[str, list[PolicyBundle]] = {}

    def create_version(
        self,
        name: str,
        rego_modules: dict[str, str],
        data: dict[str, Any] | None = None,
        metadata: dict[str, str] | None = None,
        created_at: float | None = None
    ) -> PolicyBundle:
        """Create a new policy bundle version."""

        # Check max bundles
        if name not in self._bundles:
            if len(self._bundles) >= MAX_POLICY_BUNDLES:
                raise ConfigError('conflict',
                    f'Maximum {MAX_POLICY_BUNDLES} bundles reached')
            self._bundles[name] = []

        versions = self._bundles[name]

        # Check max versions per bundle
        if len(versions) >= MAX_VERSIONS_PER_BUNDLE:
            raise ConfigError('conflict',
                f'Maximum {MAX_VERSIONS_PER_BUNDLE} versions reached for bundle {name}')

        # Check size limit
        import json
        rego_size = len(json.dumps(rego_modules, separators=(',', ':'), ensure_ascii=False))
        if rego_size > MAX_REGO_PAYLOAD_SIZE:
            raise ConfigError('too_large',
                f'Combined rego_modules payload exceeds {MAX_REGO_PAYLOAD_SIZE} bytes')

        next_version = len(versions) + 1

        new_bundle = PolicyBundle(
            name=name,
            version=next_version,
            rego_modules=rego_modules,
            data=data,
            metadata=metadata,
            created_at=created_at
        )

        versions.append(new_bundle)
        return new_bundle

    def get_version(self, name: str, version: int) -> PolicyBundle:
        """Get a specific bundle version."""
        if name not in self._bundles:
            raise ConfigError('policy_not_found',
                f'Policy bundle {name} not found')

        for bundle in self._bundles[name]:
            if bundle.version == version:
                return bundle

        raise ConfigError('policy_not_found',
            f'Policy bundle {name} version {version} not found')

    def list_versions(self, name: str) -> list[PolicyBundle]:
        """List all versions for a bundle name."""
        if name not in self._bundles:
            raise ConfigError('policy_not_found',
                f'Policy bundle {name} not found')
        return list(self._bundles[name])

    def get_latest(self, name: str) -> PolicyBundle | None:
        """Get the latest bundle version, or None if not found."""
        if name not in self._bundles or not self._bundles[name]:
            return None
        return self._bundles[name][-1]


class PolicyBindingStorage:
    """In-memory storage for policy bindings."""

    def __init__(self):
        self._bindings: dict[int, PolicyBinding] = {}
        self._next_binding_id = 1

        # Indexes for fast lookup by selector
        # selector_hash -> binding_id
        self._selector_index: dict[tuple[str, int], int] = {}

    def _selector_key(self, selector: dict[str, str]) -> tuple[str, int]:
        """Create a hash key for a selector."""
        return (str(sorted(selector.items())), hash(frozenset(selector.items())))

    def create_binding(
        self,
        bundle_ref: dict[str, int],  # {"name": str, "version": int}
        selector: dict[str, str],
        graph_keys: list[str],
        priority: int,
        bundle_storage: PolicyBundleStorage
    ) -> PolicyBinding:
        """Create a new policy binding."""

        # Check max bindings
        if len(self._bindings) >= MAX_POLICY_BINDINGS:
            raise ConfigError('policy_conflict',
                f'Maximum {MAX_POLICY_BINDINGS} policy bindings reached')

        # Validate bundle exists and version matches
        bundle_name = bundle_ref.get('name')
        bundle_version = bundle_ref.get('version')

        if not bundle_name:
            raise ConfigError('invalid_input', 'Bundle reference must have "name"')
        if bundle_version is None:
            raise ConfigError('invalid_input', 'Bundle reference must have "version"')

        try:
            bundle = bundle_storage.get_version(bundle_name, bundle_version)
        except ConfigError as e:
            if e.code == 'not_found':
                raise ConfigError('policy_not_found',
                    f'Policy bundle {bundle_name} version {bundle_version} not found')
            raise

        # Validate selector is non-empty exact-match map
        if not selector:
            raise ConfigError('invalid_input', 'Selector must be a non-empty object')

        for k, v in selector.items():
            if not isinstance(k, str):
                raise ConfigError('invalid_input', 'Selector keys must be strings')
            if not isinstance(v, str):
                raise ConfigError('invalid_input', 'Selector values must be strings')

        # Validate graph_keys
        if not isinstance(graph_keys, list):
            raise ConfigError('invalid_input', 'graph_keys must be a list')

        for key in graph_keys:
            if not isinstance(key, str):
                raise ConfigError('invalid_input', 'graph_keys elements must be strings')

        # Validate priority
        if not isinstance(priority, int):
            raise ConfigError('invalid_input', 'priority must be an integer')

        # Check for duplicate selector for same bundle+priority
        selector_key = self._selector_key(selector)

        # Check for conflicts
        for binding in self._bindings.values():
            # If same bundle and priority, check same selector
            if (binding.bundle.get('name') == bundle_name and
                binding.bundle.get('version') == bundle_version and
                binding.priority == priority):
                if binding.selector == selector:
                    raise ConfigError('policy_conflict',
                        'Duplicate binding for same bundle and selector with same priority')

        binding = PolicyBinding(
            binding_id=self._next_binding_id,
            bundle={'name': bundle_name, 'version': bundle_version},
            selector=selector,
            graph_keys=list(graph_keys),
            priority=priority
        )

        self._bindings[self._next_binding_id] = binding
        self._next_binding_id += 1

        return binding

    def get_binding(self, binding_id: int) -> PolicyBinding | None:
        """Get a binding by ID."""
        return self._bindings.get(binding_id)

    def list_bindings(self) -> list[PolicyBinding]:
        """List all bindings sorted by priority then bundle name/version."""
        bindings = list(self._bindings.values())
        return sorted(bindings, key=lambda b: (-b.priority, b.bundle['name'], b.bundle['version']))

    def find_matching_bindings(
        self,
        target_scope: dict[str, str]
    ) -> list[PolicyBinding]:
        """Find all bindings whose selectors match the target scope."""
        matching = []
        for binding in self._bindings.values():
            selector = binding.selector
            # All selector keys must match
            match = True
            for k, v in selector.items():
                if target_scope.get(k) != v:
                    match = False
                    break
            if match:
                matching.append(binding)

        # Sort by priority descending, then bundle name ascending, then version ascending
        return sorted(matching, key=lambda b: (-b.priority, b.bundle['name'], b.bundle['version']))

    def delete_binding(self, binding_id: int) -> None:
        """Delete a binding by ID."""
        if binding_id in self._bindings:
            del self._bindings[binding_id]
