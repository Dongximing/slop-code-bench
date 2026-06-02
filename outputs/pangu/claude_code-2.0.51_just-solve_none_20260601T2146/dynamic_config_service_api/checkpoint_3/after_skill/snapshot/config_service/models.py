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
