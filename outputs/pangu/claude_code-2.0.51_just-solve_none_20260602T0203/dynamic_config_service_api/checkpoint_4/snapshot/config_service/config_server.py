#!/usr/bin/env python3
"""
Configuration Management Service - Immutable versioned configs with scoping,
rollback, import/inheritance support, and schema registry with validation,
plus organization-level policy guardrails with OPA/Rego evaluation.
"""

import json
import re
import base64
import tarfile
import io
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Any, Dict, List, Tuple, Set, Union
from flask import Flask, request, Response
import argparse

# Rego policy engine
from rego import Rego

app = Flask(__name__)

@dataclass(frozen=True)
class Scope:
    values: Tuple[Tuple[str, str], ...] = field(default_factory=tuple)

    def __init__(self, d: Optional[Dict[str, str]] = None):
        if d is None:
            d = {}
        if not isinstance(d, dict):
            raise ValueError("Scope must be a dict")
        for k, v in d.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise ValueError("Scope keys and values must be strings")
        object.__setattr__(self, 'values', tuple(sorted(d.items())))

    def to_dict(self) -> Dict[str, str]:
        return dict(self.values)

    def __hash__(self):
        return hash(self.values)

    def __eq__(self, other):
        if not isinstance(other, Scope):
            return False
        return self.values == other.values


@dataclass(frozen=True)
class IncludeRef:
    name: str
    scope: Scope
    version: Optional[int]  # None means use active at resolution time

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("Include name must be non-empty string")
        if not isinstance(self.scope, Scope):
            raise ValueError("Include scope must be a Scope")
        if self.version is not None and (not isinstance(self.version, int) or self.version < 1):
            raise ValueError("Include version must be a positive integer or null")

    def to_dict(self) -> Dict[str, Any]:
        result = {"name": self.name, "scope": self.scope.to_dict()}
        if self.version is not None:
            result["version"] = self.version
        return result

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'IncludeRef':
        if not isinstance(d, dict):
            raise ValueError("Include reference must be an object")
        if "name" not in d:
            raise ValueError("Include reference must have 'name'")
        if "scope" not in d:
            raise ValueError("Include reference must have 'scope'")
        name = d["name"]
        scope = Scope(d["scope"])
        version = d.get("version")
        if version is not None and not isinstance(version, int):
            raise ValueError("Include version must be integer or null")
        return cls(name=name, scope=scope, version=version)


@dataclass(frozen=True)
class Config:
    config: Dict[str, Any]
    includes: Tuple[IncludeRef, ...]
    version: int
    status: str = "active"  # "active" or "draft"

    def __post_init__(self):
        if not isinstance(self.config, dict):
            raise ValueError("Config must be a dict")
        if not isinstance(self.includes, tuple):
            object.__setattr__(self, 'includes', tuple(self.includes))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "config": self.config,
            "includes": [inc.to_dict() for inc in self.includes]
        }


@dataclass(frozen=True)
class ConfigKey:
    name: str
    scope: Scope

    def __hash__(self):
        return hash((self.name, self.scope))

    def __eq__(self, other):
        if not isinstance(other, ConfigKey):
            return False
        return self.name == other.name and self.scope == other.scope


# Workflow Data Models

@dataclass(frozen=True)
class ApprovalPolicy:
    """Policy for approval quorum."""
    required_approvals: int
    allow_author_approval: bool
    allowed_reviewers: Optional[Tuple[str, ...]]  # None means any actor allowed
    scope: Scope

    def __init__(self, required_approvals: int, allow_author_approval: bool,
                 allowed_reviewers: Optional[List[str]], scope: Scope):
        if allowed_reviewers is not None:
            # Validate unique and sorted
            object.__setattr__(self, 'allowed_reviewers', tuple(sorted(set(allowed_reviewers))))
        else:
            object.__setattr__(self, 'allowed_reviewers', None)
        object.__setattr__(self, 'required_approvals', required_approvals)
        object.__setattr__(self, 'allow_author_approval', allow_author_approval)
        object.__setattr__(self, 'scope', scope)

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "required_approvals": self.required_approvals,
            "allow_author_approval": self.allow_author_approval
        }
        if self.allowed_reviewers is not None:
            result["allowed_reviewers"] = list(self.allowed_reviewers)
        result["scope"] = self.scope.to_dict()
        return result


class ProposalStatus(Enum):
    """Proposal lifecycle states."""
    OPEN = "open"
    APPROVED = "approved"
    REJECTED = "rejected"
    MERGED = "merged"
    WITHDRAWN = "withdrawn"
    SUPERSEDED = "superseded"


@dataclass(frozen=True)
class Tally:
    """Tracks approvals and rejections for a proposal."""
    approvals: int  # count of distinct approving actors
    rejections: int  # count of distinct rejecting actors
    by_actor: Dict[str, str]  # actor -> decision ('approve' or 'reject')

    def __init__(self):
        object.__setattr__(self, 'approvals', 0)
        object.__setattr__(self, 'rejections', 0)
        object.__setattr__(self, 'by_actor', {})

    def with_decision(self, actor: str, decision: str) -> 'Tally':
        """Return a new Tally with the decision applied."""
        new_by_actor = dict(self.by_actor)
        old_decision = new_by_actor.get(actor)

        new_tally = Tally()
        object.__setattr__(new_tally, 'by_actor', new_by_actor)

        # Count existing approvals/rejections from other actors
        approvals = 0
        rejections = 0
        for a, d in new_by_actor.items():
            if a == actor:
                continue
            if d == 'approve':
                approvals += 1
            elif d == 'reject':
                rejections += 1

        # Apply new decision
        new_by_actor[actor] = decision
        if decision == 'approve':
            approvals += 1
        elif decision == 'reject':
            rejections += 1

        object.__setattr__(new_tally, 'approvals', approvals)
        object.__setattr__(new_tally, 'rejections', rejections)
        return new_tally

    def to_dict(self) -> Dict[str, Any]:
        return {
            "approvals": self.approvals,
            "rejections": self.rejections,
            "by_actor": dict(sorted(self.by_actor.items()))
        }


@dataclass(frozen=True)
class Review:
    """A single review by an actor."""
    actor: str
    decision: str  # 'approve' or 'reject'
    message: Optional[str]
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class DiffArtifacts:
    """Diff surfaces between draft and base versions."""
    raw_json_patch: List[Dict[str, Any]]
    resolved_json_patch: List[Dict[str, Any]]
    includes_changes: List[Dict[str, Any]]
    human: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "raw_json_patch": self.raw_json_patch,
            "resolved_json_patch": self.resolved_json_patch,
            "includes_changes": self.includes_changes,
            "human": self.human
        }


@dataclass(frozen=True)
class Proposal:
    """A proposal to merge a draft config."""
    proposal_id: int
    name: str
    scope: Scope
    draft_version: int
    base_version: int
    author: str
    title: Optional[str]
    description: Optional[str]
    labels: Tuple[str, ...]
    quorum: ApprovalPolicy
    status: ProposalStatus
    tally: Tally
    diffs: DiffArtifacts
    reviews: Tuple[Review, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "name": self.name,
            "scope": self.scope.to_dict(),
            "draft_version": self.draft_version,
            "base_version": self.base_version,
            "author": self.author,
            "title": self.title,
            "description": self.description,
            "labels": list(sorted(self.labels)),
            "quorum": self.quorum.to_dict(),
            "status": self.status.value,
            "tally": self.tally.to_dict(),
            "diffs": self.diffs.to_dict(),
            "reviews": [{
                "actor": r.actor,
                "decision": r.decision,
                "message": r.message
            } for r in self.reviews]
        }


# Storage

class ConfigStorage:
    """In-memory storage for configs."""

    def __init__(self):
        # key -> { version -> Config }
        self._configs: Dict[ConfigKey, Dict[int, Config]] = {}
        # key -> active version
        self._active: Dict[ConfigKey, int] = {}
        # key -> next version number
        self._next_version: Dict[ConfigKey, int] = {}

    def create_version(self, name: str, scope: Scope, config: Dict[str, Any],
                       includes: List[IncludeRef], allow_overflow: bool = True) -> Tuple[int, bool]:
        """
        Create a new immutable version.
        Returns (version, was_active).
        """
        key = ConfigKey(name, scope)

        if key not in self._next_version:
            self._next_version[key] = 1
            self._configs[key] = {}

        current_next = self._next_version[key]
        if len(self._configs[key]) >= 10000:
            raise ValueError("Max versions exceeded")

        version = current_next
        config_obj = Config(config=config, includes=tuple(includes), version=version)

        self._configs[key][version] = config_obj
        self._next_version[key] = version + 1

        # Check if this should be active
        was_active = key not in self._active or version > self._active[key]
        if was_active:
            self._active[key] = version

        return version, was_active

    def get_versions(self, name: str, scope: Scope) -> List[Dict[str, Any]]:
        """Get all versions for a key."""
        key = ConfigKey(name, scope)
        if key not in self._configs:
            return []

        active_version = self._active.get(key)
        result = []
        for v in sorted(self._configs[key].keys()):
            result.append({
                "version": v,
                "active": v == active_version
            })
        return result

    def get_version(self, name: str, scope: Scope, version: int) -> Optional[Config]:
        """Get a specific version."""
        key = ConfigKey(name, scope)
        if key not in self._configs:
            return None
        return self._configs[key].get(version)

    def get_active(self, name: str, scope: Scope) -> Optional[Config]:
        """Get the active version."""
        key = ConfigKey(name, scope)
        if key not in self._active:
            return None
        return self._configs[key].get(self._active[key])

    def activate_version(self, name: str, scope: Scope, version: int) -> bool:
        """
        Activate a specific version.
        Returns True if version existed and was activated.
        """
        key = ConfigKey(name, scope)
        if key not in self._configs or version not in self._configs[key]:
            return False
        self._active[key] = version
        return True

    def version_exists(self, name: str, scope: Scope, version: int) -> bool:
        """Check if a version exists."""
        key = ConfigKey(name, scope)
        return key in self._configs and version in self._configs[key]

    def get_active_version(self, name: str, scope: Scope) -> Optional[int]:
        """Get the active version number."""
        key = ConfigKey(name, scope)
        return self._active.get(key)


# =============================================================================
# Policy Bundle Data Models
# =============================================================================

@dataclass(frozen=True)
class PolicyBundle:
    """Versioned policy bundle containing Rego modules and data."""
    name: str
    version: int
    rego_modules: Dict[str, str]
    data: Dict[str, Any]
    metadata: Dict[str, Any]

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("Bundle name must be non-empty string")
        if not isinstance(self.version, int) or self.version < 1:
            raise ValueError("Bundle version must be a positive integer")
        if not isinstance(self.rego_modules, dict):
            raise ValueError("rego_modules must be a dict")
        if not isinstance(self.data, dict):
            raise ValueError("data must be a dict")
        if not isinstance(self.metadata, dict):
            raise ValueError("metadata must be a dict")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "metadata": self.metadata
        }

    def to_full_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "rego_modules": dict(self.rego_modules),
            "data": dict(self.data),
            "metadata": dict(self.metadata)
        }


@dataclass(frozen=True)
class PolicyBinding:
    """Binding associates a bundle version with a selector and defines graph behavior."""
    binding_id: int
    bundle_name: str
    bundle_version: int
    selector: Dict[str, str]
    graph_keys: Tuple[str, ...]
    priority: int

    def __post_init__(self):
        if not isinstance(self.binding_id, int) or self.binding_id < 1:
            raise ValueError("binding_id must be a positive integer")
        if not isinstance(self.bundle_name, str) or not self.bundle_name:
            raise ValueError("bundle_name must be non-empty string")
        if not isinstance(self.bundle_version, int) or self.bundle_version < 1:
            raise ValueError("bundle_version must be a positive integer")
        if not isinstance(self.selector, dict) or len(self.selector) == 0:
            raise ValueError("selector must be a non-empty dict")
        if not isinstance(self.graph_keys, tuple):
            object.__setattr__(self, 'graph_keys', tuple(self.graph_keys))
        if not isinstance(self.priority, int):
            raise ValueError("priority must be an integer")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "binding_id": self.binding_id,
            "bundle": {"name": self.bundle_name, "version": self.bundle_version},
            "selector": dict(self.selector),
            "graph_keys": list(self.graph_keys),
            "priority": self.priority
        }


@dataclass(frozen=True)
class PolicyEvaluationResult:
    """Result of policy evaluation."""
    policy_stack: List[Dict[str, Any]]
    violations: List[Dict[str, Any]]
    tally: Dict[str, int]
    truncated: bool = False
    graph_truncated: bool = False

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "policy_stack": self.policy_stack,
            "violations": self.violations,
            "tally": self.tally
        }
        if self.truncated:
            result["truncated"] = True
        if self.graph_truncated:
            result["details"] = {"graph_truncated": True}
        return result


# =============================================================================
# Policy Storage
# =============================================================================

class PolicyStorage:
    """In-memory storage for policy bundles and bindings."""

    def __init__(self, max_bundles: int = 500, max_versions: int = 200,
                 max_bindings: int = 5000):
        self.max_bundles = max_bundles
        self.max_versions = max_versions
        self.max_bindings = max_bindings

        # bundle_name -> { version -> PolicyBundle }
        self._bundles: Dict[str, Dict[int, PolicyBundle]] = {}
        # bundle_name -> next version
        self._next_bundle_version: Dict[str, int] = {}

        # binding_id -> PolicyBinding
        self._bindings: Dict[int, PolicyBinding] = {}
        # next binding id
        self._next_binding_id: int = 1

    # --- Bundle operations ---

    def create_bundle(self, name: str, rego_modules: Dict[str, str],
                      data: Dict[str, Any], metadata: Dict[str, Any]) -> PolicyBundle:
        """Create a new bundle version."""
        if len(self._bundles) >= self.max_bundles:
            raise ValueError(f"Max bundles ({self.max_bundles}) exceeded")

        if name not in self._next_bundle_version:
            self._next_bundle_version[name] = 1
            self._bundles[name] = {}

        version = self._next_bundle_version[name]
        if len(self._bundles[name]) >= self.max_versions:
            raise ValueError(f"Max versions ({self.max_versions}) exceeded for bundle")

        bundle = PolicyBundle(
            name=name,
            version=version,
            rego_modules=rego_modules,
            data=data,
            metadata=metadata
        )
        self._bundles[name][version] = bundle
        self._next_bundle_version[name] = version + 1
        return bundle

    def get_bundle(self, name: str, version: int) -> Optional[PolicyBundle]:
        """Get a specific bundle version."""
        if name not in self._bundles:
            return None
        return self._bundles[name].get(version)

    def list_bundle_versions(self, name: str) -> List[Dict[str, Any]]:
        """List all versions of a bundle ascending."""
        if name not in self._bundles:
            return []
        result = []
        for v in sorted(self._bundles[name].keys()):
            result.append(self._bundles[name][v].to_dict())
        return result

    def bundle_exists(self, name: str, version: int) -> bool:
        """Check if bundle exists."""
        if name not in self._bundles:
            return False
        return version in self._bundles[name]

    # --- Binding operations ---

    def create_binding(self, bundle_name: str, bundle_version: int,
                       selector: Dict[str, str], graph_keys: List[str],
                       priority: int) -> PolicyBinding:
        """Create a new binding."""
        if len(self._bindings) >= self.max_bindings:
            raise ValueError("Max bindings exceeded")

        # Check bundle exists
        if not self.bundle_exists(bundle_name, bundle_version):
            raise ValueError(f"Bundle {bundle_name} version {bundle_version} not found")

        # Check for duplicate selector+priority
        for binding in self._bindings.values():
            if (binding.bundle_name == bundle_name and
                binding.bundle_version == bundle_version and
                binding.selector == selector and
                binding.priority == priority):
                raise ValueError("Duplicate binding for same selector and priority")

        binding_id = self._next_binding_id
        self._next_binding_id += 1

        binding = PolicyBinding(
            binding_id=binding_id,
            bundle_name=bundle_name,
            bundle_version=bundle_version,
            selector=selector,
            graph_keys=tuple(graph_keys),
            priority=priority
        )
        self._bindings[binding_id] = binding
        return binding

    def get_binding(self, binding_id: int) -> Optional[PolicyBinding]:
        """Get a binding by ID."""
        return self._bindings.get(binding_id)

    def list_bindings(self) -> List[PolicyBinding]:
        """List all bindings."""
        return list(self._bindings.values())

    def delete_binding(self, binding_id: int) -> bool:
        """Delete a binding. Returns True if existed."""
        if binding_id in self._bindings:
            del self._bindings[binding_id]
            return True
        return False

    def find_matching_bindings(self, scope: Dict[str, str]) -> List[PolicyBinding]:
        """Find all bindings whose selector matches the scope exactly."""
        matching = []
        for binding in self._bindings.values():
            # Exact match required - selector keys must all match
            match = True
            for k, v in binding.selector.items():
                if scope.get(k) != v:
                    match = False
                    break
            if match:
                matching.append(binding)
        # Sort by priority desc, then bundle name asc, then version asc
        matching.sort(key=lambda b: (-b.priority, b.bundle_name, b.bundle_version))
        return matching


# =============================================================================
# Policy Engine
# =============================================================================

class PolicyEngine:
    """OPA-based policy evaluation engine."""

    EVALUATION_TIMEOUT_MS = 500
    MAX_VIOLATIONS = 1000
    GRAPH_SIZE_CAP = 2000

    def __init__(self, policy_storage: PolicyStorage, config_storage: ConfigStorage):
        self.policy_storage = policy_storage
        self.config_storage = config_storage
        self._query_cache: Dict[str, Any] = {}
        self._query_cache_lock = threading.Lock()

    def _build_query(self, bundle: PolicyBundle) -> Rego:
        """Build a Rego query from a bundle."""
        query = Rego()
        # Add modules
        for name, code in bundle.rego_modules.items():
            query.module(name, code)
        # Add data
        if bundle.data:
            query.data(bundle.data)
        return query

    def _build_evaluation_context(self, target_name: str, target_scope: Scope,
                                   version_used: int, graph: Dict[str, Any],
                                   now: str) -> Dict[str, Any]:
        """Build the evaluation context for OPA."""
        # Build graph.by_name
        by_name = {}
        sorted_names = sorted(graph.keys())
        for name in sorted_names:
            node = graph[name]
            by_name[name] = {
                "resolved_config": node.get("resolved_config", {}),
                "provenance": node.get("provenance", {})
            }

        return {
            "target": {
                "name": target_name,
                "scope": target_scope.to_dict(),
                "version_used": version_used,
                "resolved_config": graph.get(target_name, {}).get("resolved_config", {})
            },
            "graph": {
                "by_name": by_name
            },
            "now": now
        }

    def evaluate(self, target_name: str, target_scope: Scope,
                 version_used: int, graph: Dict[str, Any],
                 bindings: List[PolicyBinding],
                 now: str) -> PolicyEvaluationResult:
        """
        Evaluate policies against a target.
        Returns PolicyEvaluationResult with violations.
        """
        policy_stack = []
        all_violations = []
        truncated = False
        graph_truncated = len(graph) > self.GRAPH_SIZE_CAP

        # Build policy stack
        for binding in bindings:
            bundle = self.policy_storage.get_bundle(
                binding.bundle_name, binding.bundle_version
            )
            if bundle is None:
                continue
            policy_stack.append({
                "bundle": {"name": bundle.name, "version": bundle.version},
                "selector": dict(binding.selector),
                "graph_keys": list(binding.graph_keys),
                "priority": binding.priority
            })

        # Evaluate each binding in order
        ctx = self._build_evaluation_context(
            target_name, target_scope, version_used, graph, now
        )

        for binding in bindings:
            bundle = self.policy_storage.get_bundle(
                binding.bundle_name, binding.bundle_version
            )
            if bundle is None:
                continue

            try:
                query = self._build_query(bundle)
                result = query.test(ctx) if hasattr(query, 'test') else None
                # Use query.eval if test not available
                if result is None:
                    result = query.eval(ctx)

                # Parse violations from result
                if result and isinstance(result, dict):
                    # Check for deny violations
                    deny_list = result.get('data', {}).get('guardrails', {}).get('deny', [])
                    warn_list = result.get('data', {}).get('guardrails', {}).get('warn', [])

                    for v in deny_list:
                        if len(all_violations) >= self.MAX_VIOLATIONS:
                            truncated = True
                            break
                        all_violations.append({
                            "policy": {"name": bundle.name, "version": bundle.version},
                            "target": {
                                "name": target_name,
                                "scope": target_scope.to_dict(),
                                "version_used": version_used
                            },
                            "rule_id": v.get("id", ""),
                            "severity": "error",
                            "path": v.get("path", ""),
                            "message": v.get("message", ""),
                            "evidence": v.get("evidence", {})
                        })

                    for v in warn_list:
                        if len(all_violations) >= self.MAX_VIOLATIONS:
                            truncated = True
                            break
                        all_violations.append({
                            "policy": {"name": bundle.name, "version": bundle.version},
                            "target": {
                                "name": target_name,
                                "scope": target_scope.to_dict(),
                                "version_used": version_used
                            },
                            "rule_id": v.get("id", ""),
                            "severity": "warn",
                            "path": v.get("path", ""),
                            "message": v.get("message", ""),
                            "evidence": v.get("evidence", {})
                        })

                    if truncated:
                        break
            except Exception:
                # Skip bindings that fail to evaluate
                continue

        # Sort violations lexicographically
        all_violations.sort(key=lambda v: (
            v["target"]["name"],
            v["policy"]["name"],
            v["policy"]["version"],
            v["rule_id"],
            v["path"]
        ))

        errors = sum(1 for v in all_violations if v["severity"] == "error")
        warnings = sum(1 for v in all_violations if v["severity"] == "warn")

        return PolicyEvaluationResult(
            policy_stack=policy_stack,
            violations=all_violations,
            tally={"errors": errors, "warnings": warnings},
            truncated=truncated,
            graph_truncated=graph_truncated
        )

    def explain_violation(self, violation: Dict[str, Any]) -> List[str]:
        """Generate explanation lines for a violation."""
        lines = []

        policy_name = violation.get("policy", {}).get("name", "")
        policy_version = violation.get("policy", {}).get("version", 0)
        target_name = violation.get("target", {}).get("name", "")
        target_scope = violation.get("target", {}).get("scope", {})
        rule_id = violation.get("rule_id", "")

        lines.append(f"Selector matched: {' '.join(f'{k}={v}' for k, v in target_scope.items())}")
        lines.append(f"Resolved {target_name} / {violation.get('path', '')} = {violation.get('evidence', {}).get('actual', 'unknown')}")
        lines.append(f"Rule {rule_id} expects {violation.get('evidence', {}).get('expected', 'unknown')}")
        lines.append(f"Decision: DENY ({violation.get('severity', 'error')})")

        return lines


# =============================================================================
# Deep Merge
# =============================================================================

def deep_merge(base: Dict[str, Any], override: Dict[str, Any], path: str = "") -> Dict[str, Any]:
    """
    Deep merge two dictionaries.
    Raises ValueError on type conflicts with path info.
    """
    result = dict(base)

    for key, value in override.items():
        current_path = f"{path}/{key}" if path else f"/{key}"

        if key in result:
            base_val = result[key]
            # Type conflict detection
            if isinstance(base_val, dict) and isinstance(value, dict):
                result[key] = deep_merge(base_val, value, current_path)
            elif isinstance(base_val, list) or isinstance(value, list):
                # Arrays replace entirely
                result[key] = value
            else:
                # Scalars replace
                if type(base_val) != type(value):
                    raise ValueError(f"Type conflict at {current_path}: {type(base_val).__name__} vs {type(value).__name__}")
                result[key] = value
        else:
            result[key] = value

    return result


# =============================================================================
# Policy Helpers
# =============================================================================

def build_resolution_graph(storage: ConfigStorage, target_name: str, target_scope: Scope,
                           version: int) -> Dict[str, Any]:
    """
    Build a graph of related configs for policy evaluation.
    Includes the target and all active configs that share scope keys.
    """
    # Get the target resolved config
    context = ResolutionContext()
    try:
        resolved, _ = resolve_config(storage, target_name, target_scope, version, context)
    except ValueError:
        resolved = {}

    target_key = ConfigKey(target_name, target_scope)

    # Build provenance info for the target
    provenance = {
        "name": target_name,
        "scope": target_scope.to_dict(),
        "version_used": version
    }

    graph = {
        target_name: {
            "resolved_config": resolved,
            "provenance": provenance
        }
    }

    # Add related configs from the storage based on scope key matching
    # We need to find all active configs that share scope keys
    for config_key in storage._configs.keys():
        # Skip the target itself
        if config_key == target_key:
            continue

        # Check if this config shares scope values on any keys that the target has
        # For simplicity in this implementation, we include all configs that have at least one matching key-value pair
        target_scope_dict = dict(target_scope.values)
        other_scope_dict = dict(config_key.scope.values)

        # Match if they share at least one scope key with same value
        has_matching_key = False
        for k, v in target_scope_dict.items():
            if other_scope_dict.get(k) == v:
                has_matching_key = True
                break

        if has_matching_key:
            active_version = storage.get_active_version(config_key.name, config_key.scope)
            if active_version is not None:
                try:
                    ctx = ResolutionContext()
                    other_resolved, _ = resolve_config(storage, config_key.name, config_key.scope,
                                                       active_version, ctx)
                    graph[config_key.name] = {
                        "resolved_config": other_resolved,
                        "provenance": {
                            "name": config_key.name,
                            "scope": config_key.scope.to_dict(),
                            "version_used": active_version
                        }
                    }
                except ValueError:
                    pass

    # Sort and truncate if needed
    sorted_names = sorted(graph.keys())
    if len(sorted_names) > PolicyEngine.GRAPH_SIZE_CAP:
        sorted_names = sorted_names[:PolicyEngine.GRAPH_SIZE_CAP]

    truncated_graph = {}
    for name in sorted_names:
        truncated_graph[name] = graph[name]

    return truncated_graph


# =============================================================================
# Include Resolution
# =============================================================================

@dataclass
class ResolutionContext:
    visited: Set[Tuple[str, Tuple[Tuple[str, str], ...], Optional[int]]] = field(default_factory=set)
    resolution_graph: List[Dict[str, Any]] = field(default_factory=list)
    max_depth: int = 64


def resolve_config(storage: ConfigStorage, name: str, scope: Scope,
                   version: Optional[int], context: ResolutionContext,
                   dry_run: bool = False) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    Resolve a config with all includes applied.
    Returns (resolved_config, resolution_graph_segment).
    """
    # Determine version to use
    if version is None:
        if dry_run:
            raise ValueError("version required for dry_run")
        version = storage.get_active_version(name, scope)
        if version is None:
            raise ValueError("no active version")

    # Check cycle
    key_tuple = (name, scope.values, version)
    if key_tuple in context.visited:
        raise ValueError("cycle_detected")

    if len(context.visited) > context.max_depth:
        raise ValueError("max_depth")

    # Add to visited
    context.visited.add(key_tuple)

    try:
        # Get config
        config = storage.get_version(name, scope, version)
        if config is None:
            if dry_run:
                return {}, []
            raise ValueError("not_found")

        # Start with empty object
        result: Dict[str, Any] = {}

        # Process includes in order
        for include_ref in config.includes:
            include_result, include_graph = resolve_config(
                storage, include_ref.name, include_ref.scope,
                include_ref.version, context, dry_run
            )
            # Merge include result
            try:
                result = deep_merge(result, include_result)
            except ValueError as e:
                if "Type conflict" in str(e):
                    raise ValueError(f"unprocessable: {str(e)}")
                raise
            # Add to resolution graph if not already there
            for node in include_graph:
                key = (node["name"], tuple(sorted(node["scope"].items())),
                       node["version_used"])
                if node not in context.resolution_graph:
                    context.resolution_graph.append(node)

        # Add current config to graph
        context.resolution_graph.append({
            "name": name,
            "scope": scope.to_dict(),
            "version_used": version
        })

        # Merge own config on top
        try:
            result = deep_merge(result, config.config)
        except ValueError as e:
            if "Type conflict" in str(e):
                raise ValueError(f"unprocessable: {str(e)}")
            raise

        return result, context.resolution_graph.copy()

    finally:
        context.visited.discard(key_tuple)


# JSON Normalization (Canonical JSON)

def canonical_json(obj: Any) -> str:
    """
    Produce canonical JSON with sorted keys, minimal representation.
    """
    def sort_keys(d):
        if isinstance(d, dict):
            return {k: sort_keys(v) for k, v in sorted(d.items())}
        if isinstance(d, list):
            return [sort_keys(item) for item in d]
        return d

    sorted_obj = sort_keys(obj)
    # Use separators without spaces
    return json.dumps(sorted_obj, separators=(',', ':'), ensure_ascii=False) + "\n"


# Error Responses

def error_response(code: str, message: str, details: Optional[Dict] = None) -> Tuple[Response, int]:
    """Create a JSON error response."""
    if details is None:
        details = {}
    body = {
        "error": {
            "code": code,
            "message": message,
            "details": details
        }
    }
    return Response(
        canonical_json(body),
        mimetype="application/json; charset=utf-8"
    ), _error_status(code)


def _error_status(code: str) -> int:
    status_map = {
        "invalid_input": 400,
        "not_found": 404,
        "conflict": 409,
        "cycle_detected": 409,
        "unprocessable": 422,
        "policy_invalid": 422,
        "policy_violation": 422,
        "policy_not_found": 404,
        "policy_conflict": 409,
        "evaluation_timeout": 408,
        "rate_limited": 429,
        "internal": 500,
        "too_large": 413
    }
    return status_map.get(code, 500)


# Request Helpers

def parse_scope(data: Dict[str, Any], field: str = "scope") -> Scope:
    """Parse scope from request data."""
    if field not in data:
        raise ValueError(f"Missing '{field}'")
    scope_data = data[field]
    if not isinstance(scope_data, dict):
        raise ValueError(f"'{field}' must be an object")
    return Scope(scope_data)


def validate_include_ref(data: Dict[str, Any]) -> IncludeRef:
    """Validate and parse an include reference."""
    return IncludeRef.from_dict(data)


def get_json_request(max_size: int = 1024 * 1024) -> Dict[str, Any]:
    """Get and validate JSON from request."""
    if request.content_length and request.content_length > max_size:
        return error_response("too_large", "Request body too large")

    if not request.is_json:
        return error_response("invalid_input", "Content-Type must be application/json")

    try:
        data = request.get_json()
        if data is None:
            return error_response("invalid_input", "Invalid JSON")
        return data
    except Exception:
        return error_response("invalid_input", "Failed to parse JSON")


# Global Storage

storage = ConfigStorage()
policy_storage = PolicyStorage()
policy_engine = PolicyEngine(policy_storage, storage)

# Idempotency tracking for create
# (name, scope_dict_str, config_str) -> version
create_cache: Dict[str, int] = {}


def get_idempotency_key(name: str, scope: Scope, config: Dict[str, Any]) -> str:
    """Generate idempotency key for create."""
    # Normalize config for comparison
    config_str = canonical_json(config)
    scope_str = canonical_json(scope.to_dict())
    return f"{name}:{scope_str}:{config_str}"


# =============================================================================
# Flask Routes
# =============================================================================

@app.route('/healthz', methods=['GET'])
def healthz():
    """Health check endpoint."""
    return Response(
        canonical_json({"ok": True}),
        mimetype="application/json; charset=utf-8"
    ), 200


@app.route('/v1/configs/<name>', methods=['POST'])
def create_config(name: str):
    """Create a new version of a config."""
    global storage, create_cache

    # Parse request
    data = get_json_request()
    if isinstance(data, tuple):
        return data  # Error response

    try:
        scope = parse_scope(data)
    except ValueError as e:
        return error_response("invalid_input", str(e))

    if "config" not in data:
        return error_response("invalid_input", "Missing 'config'")

    config_data = data["config"]
    if not isinstance(config_data, dict):
        return error_response("invalid_input", "'config' must be an object")

    includes = []
    if "includes" in data:
        if data["includes"] is not None:
            if not isinstance(data["includes"], list):
                return error_response("invalid_input", "'includes' must be a list")
            for inc_data in data["includes"]:
                try:
                    includes.append(validate_include_ref(inc_data))
                except ValueError as e:
                    return error_response("invalid_input", str(e))

    inherits_active = data.get("inherits_active", False)
    if not isinstance(inherits_active, bool):
        return error_response("invalid_input", "'inherits_active' must be boolean")

    # Check idempotency
    idempotency_key = get_idempotency_key(name, scope, config_data)
    if idempotency_key in create_cache:
        # Return existing version
        existing_version = create_cache[idempotency_key]
        key = ConfigKey(name, scope)
        active_version = storage.get_active_version(name, scope)
        return Response(
            canonical_json({
                "name": name,
                "scope": scope.to_dict(),
                "version": existing_version,
                "active": existing_version == active_version
            }),
            mimetype="application/json; charset=utf-8"
        ), 201

    # Handle inherits_active
    if inherits_active:
        active_config = storage.get_active(name, scope)
        if active_config is not None:
            # Merge active config into new config
            # Active config's values take precedence (child overrides parent)
            merged_config = deep_merge(active_config.config, config_data)
            merged_includes = list(active_config.includes) + includes
            config_data = merged_config
            includes = merged_includes

    try:
        version, is_active = storage.create_version(name, scope, config_data, includes)
    except ValueError as e:
        if "Max versions exceeded" in str(e):
            return error_response("conflict", str(e))
        return error_response("internal", str(e))

    # Cache for idempotency
    create_cache[idempotency_key] = version

    response = {
        "name": name,
        "scope": scope.to_dict(),
        "version": version,
        "active": is_active
    }
    return Response(
        canonical_json(response),
        mimetype="application/json; charset=utf-8"
    ), 201


@app.route('/v1/configs/<name>:versions', methods=['POST'])
def list_versions(name: str):
    """List all versions for a (name, scope)."""
    data = get_json_request()
    if isinstance(data, tuple):
        return data

    try:
        scope = parse_scope(data)
    except ValueError as e:
        return error_response("invalid_input", str(e))

    versions = storage.get_versions(name, scope)

    response = {
        "name": name,
        "scope": scope.to_dict(),
        "versions": versions
    }
    return Response(
        canonical_json(response),
        mimetype="application/json; charset=utf-8"
    ), 200


@app.route('/v1/configs/<name>/<int:version>', methods=['POST'])
def get_version(name: str, version: int):
    """Get a specific raw version."""
    data = get_json_request()
    if isinstance(data, tuple):
        return data

    try:
        scope = parse_scope(data)
    except ValueError as e:
        return error_response("invalid_input", str(e))

    config = storage.get_version(name, scope, version)
    if config is None:
        return error_response("not_found", f"Config {name} with version {version} not found for scope")

    active_version = storage.get_active_version(name, scope)

    response = {
        "name": name,
        "scope": scope.to_dict(),
        "version": config.version,
        "active": config.version == active_version,
        "config": config.config,
        "includes": [inc.to_dict() for inc in config.includes]
    }
    return Response(
        canonical_json(response),
        mimetype="application/json; charset=utf-8"
    ), 200


@app.route('/v1/configs/<name>:active', methods=['POST'])
def get_active(name: str):
    """Get the active raw version."""
    data = get_json_request()
    if isinstance(data, tuple):
        return data

    try:
        scope = parse_scope(data)
    except ValueError as e:
        return error_response("invalid_input", str(e))

    config = storage.get_active(name, scope)
    if config is None:
        return error_response("not_found", f"No active config found for {name} with scope")

    response = {
        "name": name,
        "scope": scope.to_dict(),
        "version": config.version,
        "active": True,
        "config": config.config,
        "includes": [inc.to_dict() for inc in config.includes]
    }
    return Response(
        canonical_json(response),
        mimetype="application/json; charset=utf-8"
    ), 200


@app.route('/v1/configs/<name>/<int:version>:activate', methods=['POST'])
def activate_version(name: str, version: int):
    """Activate a specific version."""
    data = get_json_request()
    if isinstance(data, tuple):
        return data

    try:
        scope = parse_scope(data)
    except ValueError as e:
        return error_response("invalid_input", str(e))

    if not storage.version_exists(name, scope, version):
        return error_response("not_found", f"Config {name} version {version} not found for scope")

    storage.activate_version(name, scope, version)

    response = {
        "name": name,
        "scope": scope.to_dict(),
        "version": version,
        "active": True
    }
    return Response(
        canonical_json(response),
        mimetype="application/json; charset=utf-8"
    ), 200


@app.route('/v1/configs/<name>:rollback', methods=['POST'])
def rollback(name: str):
    """Rollback to an earlier version."""
    data = get_json_request()
    if isinstance(data, tuple):
        return data

    if "to_version" not in data:
        return error_response("invalid_input", "Missing 'to_version'")

    try:
        to_version = int(data["to_version"])
    except (ValueError, TypeError):
        return error_response("invalid_input", "'to_version' must be an integer")

    try:
        scope = parse_scope(data)
    except ValueError as e:
        return error_response("invalid_input", str(e))

    # Check if target version exists
    if not storage.version_exists(name, scope, to_version):
        return error_response("not_found", f"Config {name} version {to_version} not found for scope")

    current_active = storage.get_active_version(name, scope)

    # Can only rollback to earlier or equal version
    if current_active is not None and to_version > current_active:
        return error_response("conflict", f"Cannot rollback to version {to_version}, current active is {current_active}")

    storage.activate_version(name, scope, to_version)

    response = {
        "name": name,
        "scope": scope.to_dict(),
        "version": to_version,
        "active": True
    }
    return Response(
        canonical_json(response),
        mimetype="application/json; charset=utf-8"
    ), 200


@app.route('/v1/configs/<name>:resolve', methods=['POST'])
def resolve(name: str):
    """Resolve a config with all includes applied."""
    data = get_json_request()
    if isinstance(data, tuple):
        return data

    try:
        scope = parse_scope(data)
    except ValueError as e:
        return error_response("invalid_input", str(e))

    version = data.get("version")
    if version is not None and not isinstance(version, int):
        return error_response("invalid_input", "'version' must be an integer")

    dry_run = data.get("dry_run", False)
    if not isinstance(dry_run, bool):
        return error_response("invalid_input", "'dry_run' must be boolean")

    context = ResolutionContext()

    try:
        resolved_config, resolution_graph = resolve_config(
            storage, name, scope, version, context, dry_run
        )
    except ValueError as e:
        error_msg = str(e)
        if "cycle_detected" in error_msg:
            return error_response("cycle_detected", "Cycle detected in include references")
        elif "max_depth" in error_msg:
            return error_response("unprocessable", "Max include chain depth exceeded",
                                 {"reason": "max_depth"})
        elif "not_found" in error_msg:
            # Find which config is missing
            return error_response("not_found", f"Referenced config not found for {name} with scope")
        elif "unprocessable" in error_msg:
            # Extract path from error message
            path = error_msg.split(":")[-1].strip() if ":" in error_msg else ""
            return error_response("unprocessable", "Merge type conflict",
                                 {"path": path})
        elif "version required" in error_msg:
            return error_response("invalid_input", error_msg)
        else:
            return error_response("internal", error_msg)

    # Determine version used
    if version is None:
        version_used = storage.get_active_version(name, scope)
    else:
        version_used = version

    response = {
        "name": name,
        "scope": scope.to_dict(),
        "version_used": version_used,
        "resolved_config": resolved_config,
        "resolution_graph": resolution_graph
    }
    return Response(
        canonical_json(response),
        mimetype="application/json; charset=utf-8"
    ), 200


# =============================================================================
# Policy Bundle Endpoints
# =============================================================================

@app.route('/v1/policies/bundles/<bundle_name>/versions', methods=['POST'])
def create_policy_bundle(bundle_name: str):
    """Create a new policy bundle version."""
    data = get_json_request()
    if isinstance(data, tuple):
        return data

    # Parse modules and data
    rego_modules = {}
    if 'rego_modules' in data:
        if not isinstance(data['rego_modules'], dict):
            return error_response("invalid_input", "'rego_modules' must be an object")
        for name, code in data['rego_modules'].items():
            if not isinstance(name, str) or not isinstance(code, str):
                return error_response("invalid_input", "Module names and code must be strings")
        rego_modules = data['rego_modules']

    # Handle tarball option
    if 'tarball_b64' in data:
        try:
            tar_data = base64.b64decode(data['tarball_b64'])
            tar_buffer = io.BytesIO(tar_data)
            with tarfile.open(fileobj=tar_buffer, mode='r:gz') as tar:
                for member in tar.getmembers():
                    if member.isfile():
                        f = tar.extractfile(member)
                        if f:
                            content = f.read().decode('utf-8')
                            rego_modules[member.name] = content
        except Exception as e:
            return error_response("policy_invalid", f"Failed to parse tarball: {str(e)}")

    # Validate required entrypoints
    if not rego_modules:
        return error_response("policy_invalid", "No Rego modules provided")

    # Check for package guardrails
    has_guardrails = False
    for code in rego_modules.values():
        if 'package guardrails' in code or 'package guardrails.' in code:
            has_guardrails = True
            break

    if not has_guardrails:
        return error_response("policy_invalid", "Missing required package guardrails")

    # Validate size limit (1 MiB)
    size_estimate = sum(len(code) for code in rego_modules.values())
    if size_estimate > 1024 * 1024:
        return error_response("too_large", "Combined rego_modules payload exceeds 1 MiB")

    # Parse optional fields
    data_field = data.get('data', {})
    if not isinstance(data_field, dict):
        return error_response("invalid_input", "'data' must be an object")

    metadata = data.get('metadata', {})
    if not isinstance(metadata, dict):
        return error_response("invalid_input", "'metadata' must be an object")

    # Create bundle
    try:
        bundle = policy_storage.create_bundle(
            name=bundle_name,
            rego_modules=rego_modules,
            data=data_field,
            metadata=metadata
        )
    except ValueError as e:
        if "Max bundles" in str(e):
            return error_response("policy_conflict", str(e))
        elif "Max versions" in str(e):
            return error_response("policy_conflict", str(e))
        else:
            return error_response("internal", str(e))

    response = {
        "bundle_name": bundle.name,
        "version": bundle.version
    }
    return Response(
        canonical_json(response),
        mimetype="application/json; charset=utf-8"
    ), 201


@app.route('/v1/policies/bundles/<bundle_name>/versions:list', methods=['POST'])
def list_policy_bundle_versions(bundle_name: str):
    """List all versions of a policy bundle."""
    get_json_request()  # Validate JSON but ignore body

    versions = policy_storage.list_bundle_versions(bundle_name)

    response = {
        "bundle_name": bundle_name,
        "versions": versions
    }
    return Response(
        canonical_json(response),
        mimetype="application/json; charset=utf-8"
    ), 200


@app.route('/v1/policies/bundles/<bundle_name>/versions/<int:version>:get', methods=['POST'])
def get_policy_bundle_version(bundle_name: str, version: int):
    """Get a specific policy bundle version."""
    get_json_request()  # Validate JSON but ignore body

    bundle = policy_storage.get_bundle(bundle_name, version)
    if bundle is None:
        return error_response("policy_not_found",
            f"Bundle {bundle_name} version {version} not found")

    response = bundle.to_full_dict()
    return Response(
        canonical_json(response),
        mimetype="application/json; charset=utf-8"
    ), 200


# =============================================================================
# Policy Binding Endpoints
# =============================================================================

@app.route('/v1/policies/bindings', methods=['POST'])
def create_policy_binding():
    """Create a new policy binding."""
    data = get_json_request()
    if isinstance(data, tuple):
        return data

    # Validate bundle reference
    if 'bundle' not in data:
        return error_response("invalid_input", "Missing 'bundle'")

    bundle_spec = data['bundle']
    if not isinstance(bundle_spec, dict):
        return error_response("invalid_input", "'bundle' must be an object")

    if 'name' not in bundle_spec or 'version' not in bundle_spec:
        return error_response("invalid_input", "'bundle' must have 'name' and 'version'")

    bundle_name = bundle_spec['name']
    bundle_version = bundle_spec['version']

    if not isinstance(bundle_name, str) or not isinstance(bundle_version, int):
        return error_response("invalid_input", "'bundle.name' must be string, 'bundle.version' must be integer")

    # Validate selector
    if 'selector' not in data:
        return error_response("invalid_input", "Missing 'selector'")

    selector = data['selector']
    if not isinstance(selector, dict) or len(selector) == 0:
        return error_response("invalid_input", "'selector' must be a non-empty dict")

    for k, v in selector.items():
        if not isinstance(k, str) or not isinstance(v, str):
            return error_response("invalid_input", "Selector keys and values must be strings")

    # Validate graph_keys (optional, default ["env", "tenant"])
    graph_keys = data.get('graph_keys', ["env", "tenant"])
    if not isinstance(graph_keys, list) or len(graph_keys) == 0:
        return error_response("invalid_input", "'graph_keys' must be a non-empty list")
    for k in graph_keys:
        if not isinstance(k, str):
            return error_response("invalid_input", "graph_keys must be strings")

    # Validate priority
    if 'priority' not in data:
        return error_response("invalid_input", "Missing 'priority'")

    priority = data['priority']
    if not isinstance(priority, int):
        return error_response("invalid_input", "'priority' must be an integer")

    # Create binding
    try:
        binding = policy_storage.create_binding(
            bundle_name=bundle_name,
            bundle_version=bundle_version,
            selector=selector,
            graph_keys=graph_keys,
            priority=priority
        )
    except ValueError as e:
        if "not found" in str(e):
            return error_response("policy_not_found", str(e))
        elif "Duplicate binding" in str(e):
            return error_response("policy_conflict", str(e))
        elif "Max bindings" in str(e):
            return error_response("policy_conflict", str(e))
        else:
            return error_response("internal", str(e))

    response = binding.to_dict()
    return Response(
        canonical_json(response),
        mimetype="application/json; charset=utf-8"
    ), 201


# =============================================================================
# Policy Stack Endpoint
# =============================================================================

@app.route('/v1/policies/stack', methods=['POST'])
def get_policy_stack():
    """Get the policy stack that would apply to a target."""
    data = get_json_request()
    if isinstance(data, tuple):
        return data

    if 'name' not in data or 'scope' not in data:
        return error_response("invalid_input", "Missing 'name' or 'scope'")

    name = data['name']
    scope_dict = data['scope']

    if not isinstance(name, str) or not isinstance(scope_dict, dict):
        return error_response("invalid_input", "'name' must be string, 'scope' must be object")

    # Find matching bindings
    matching_bindings = policy_storage.find_matching_bindings(scope_dict)

    # Build stack
    policy_stack = []
    for binding in matching_bindings:
        bundle = policy_storage.get_bundle(
            binding.bundle_name, binding.bundle_version
        )
        if bundle is not None:
            policy_stack.append({
                "bundle": {"name": bundle.name, "version": bundle.version},
                "selector": dict(binding.selector),
                "graph_keys": list(binding.graph_keys),
                "priority": binding.priority
            })

    response = {
        "name": name,
        "scope": scope_dict,
        "policy_stack": policy_stack
    }
    return Response(
        canonical_json(response),
        mimetype="application/json; charset=utf-8"
    ), 200


# =============================================================================
# Policy Evaluate Endpoint
# =============================================================================

@app.route('/v1/policies/evaluate', methods=['POST'])
def evaluate_policies():
    """Evaluate policies on a target config."""
    data = get_json_request()
    if isinstance(data, tuple):
        return data

    if 'name' not in data or 'scope' not in data:
        return error_response("invalid_input", "Missing 'name' or 'scope'")

    name = data['name']
    scope_dict = data['scope']

    if not isinstance(name, str) or not isinstance(scope_dict, dict):
        return error_response("invalid_input", "'name' must be string, 'scope' must be object")

    version = data.get('version')
    if version is not None and not isinstance(version, int):
        return error_response("invalid_input", "'version' must be an integer")

    include_graph = data.get('include_graph', True)
    if not isinstance(include_graph, bool):
        return error_response("invalid_input", "'include_graph' must be boolean")

    try:
        scope = Scope(scope_dict)
    except ValueError as e:
        return error_response("invalid_input", str(e))

    # Determine version to use
    if version is None:
        version = storage.get_active_version(name, scope)
        if version is None:
            return error_response("not_found",
                f"No active config found for {name} with scope")

    # Verify version exists
    if not storage.version_exists(name, scope, version):
        return error_response("not_found",
            f"Config {name} version {version} not found for scope")

    # Find matching bindings
    bindings = policy_storage.find_matching_bindings(scope_dict)

    # Build resolution graph
    graph = build_resolution_graph(storage, name, scope, version)

    # Get server UTC timestamp
    now = time.time()
    now_rfc3339 = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))

    # Evaluate policies
    try:
        result = policy_engine.evaluate(name, scope, version, graph, bindings, now_rfc3339)
    except Exception as e:
        return error_response("internal", f"Policy evaluation failed: {str(e)}")

    # Build response
    response = result.to_dict()
    if include_graph:
        # Add graph info
        response["graph"] = graph
        if result.graph_truncated:
            if "details" not in response:
                response["details"] = {}
            response["details"]["graph_truncated"] = True

    return Response(
        canonical_json(response),
        mimetype="application/json; charset=utf-8"
    ), 200


# =============================================================================
# Policy Explain Endpoint
# =============================================================================

@app.route('/v1/policies/explain', methods=['POST'])
def explain_violation():
    """Explain why a violation fired."""
    data = get_json_request()
    if isinstance(data, tuple):
        return data

    if 'violation' not in data:
        return error_response("invalid_input", "Missing 'violation'")

    violation = data['violation']
    if not isinstance(violation, dict):
        return error_response("invalid_input", "'violation' must be an object")

    # Generate explanation
    explain_lines = policy_engine.explain_violation(violation)

    response = {
        "explain": explain_lines
    }
    return Response(
        canonical_json(response),
        mimetype="application/json; charset=utf-8"
    ), 200


# =============================================================================
# Proposal Management (extended with policy enforcement)
# =============================================================================

# Storage for proposals
proposals: Dict[int, 'Proposal'] = {}
next_proposal_id: int = 1
proposals_lock = threading.Lock()


@dataclass
class ProposalData:
    """Internal proposal tracking data."""
    proposal_id: int
    name: str
    scope: Scope
    draft_version: int
    base_version: int
    author: str
    title: Optional[str]
    description: Optional[str]
    labels: Tuple[str, ...]
    quorum: ApprovalPolicy
    status: ProposalStatus
    tally: Tally
    diffs: DiffArtifacts
    reviews: Tuple[Review, ...]
    policy_summary: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "proposal_id": self.proposal_id,
            "name": self.name,
            "scope": self.scope.to_dict(),
            "draft_version": self.draft_version,
            "base_version": self.base_version,
            "author": self.author,
            "title": self.title,
            "description": self.description,
            "labels": list(sorted(self.labels)),
            "quorum": self.quorum.to_dict(),
            "status": self.status.value,
            "tally": self.tally.to_dict(),
            "diffs": self.diffs.to_dict(),
            "reviews": [{
                "actor": r.actor,
                "decision": r.decision,
                "message": r.message
            } for r in self.reviews]
        }
        if self.policy_summary is not None:
            result["policy_summary"] = self.policy_summary
        return result


def compute_diff(base_config: Dict[str, Any], draft_config: Dict[str, Any]) -> DiffArtifacts:
    """Compute the diff between base and draft configs."""
    # Simplified diff computation - in production would use jsonpatch or similar
    raw_patch = []

    def compare_dicts(base, draft, path=""):
        for key in set(base.keys()) | set(draft.keys()):
            current_path = f"/{key}" if not path else f"{path}/{key}"
            if key not in base:
                raw_patch.append({"op": "add", "path": current_path, "value": draft[key]})
            elif key not in draft:
                raw_patch.append({"op": "remove", "path": current_path})
            elif isinstance(base[key], dict) and isinstance(draft[key], dict):
                compare_dicts(base[key], draft[key], current_path)
            elif base[key] != draft[key]:
                raw_patch.append({"op": "replace", "path": current_path, "value": draft[key]})

    compare_dicts(base_config, draft_config)

    human = []
    for patch in raw_patch:
        if patch["op"] == "add":
            human.append(f"+{patch['path']} = {json.dumps(patch['value'])}")
        elif patch["op"] == "remove":
            human.append(f"-{patch['path']}")
        elif patch["op"] == "replace":
            human.append(f"~{patch['path']}: {json.dumps(base.get(patch['path'].split('/')[-1], '???'))} -> {json.dumps(patch['value'])}")

    return DiffArtifacts(
        raw_json_patch=raw_patch,
        resolved_json_patch=raw_patch,
        includes_changes=[],
        human=human
    )


def compute_resolution_diff(base_resolved: Dict[str, Any], draft_resolved: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Compute diff between resolved configs."""
    return compute_diff(base_resolved, draft_resolved).raw_json_patch


def compute_quorum(requirement: Dict[str, Any], scope: Scope) -> ApprovalPolicy:
    """Compute approval quorum from requirement spec."""
    return ApprovalPolicy(
        required_approvals=requirement.get("required_approvals", 1),
        allow_author_approval=requirement.get("allow_author_approval", True),
        allowed_reviewers=requirement.get("allowed_reviewers"),
        scope=scope
    )


def run_policy_on_propose(name: str, scope: Scope, version: int) -> tuple:
    """Run policy evaluation for proposal. Returns (result, graph_truncated)."""
    scope_dict = scope.to_dict()
    bindings = policy_storage.find_matching_bindings(scope_dict)
    graph = build_resolution_graph(storage, name, scope, version)

    now = time.time()
    now_rfc3339 = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))

    result = policy_engine.evaluate(name, scope, version, graph, bindings, now_rfc3339)
    return result, result.graph_truncated


@app.route('/v1/configs/<name>:propose', methods=['POST'])
def propose_config(name: str):
    """Propose a new version for review with policy enforcement."""
    global next_proposal_id, proposals

    data = get_json_request()
    if isinstance(data, tuple):
        return data

    try:
        scope = parse_scope(data)
    except ValueError as e:
        return error_response("invalid_input", str(e))

    if "config" not in data:
        return error_response("invalid_input", "Missing 'config'")

    config_data = data["config"]
    if not isinstance(config_data, dict):
        return error_response("invalid_input", "'config' must be an object")

    includes = []
    if "includes" in data:
        if data["includes"] is not None:
            if not isinstance(data["includes"], list):
                return error_response("invalid_input", "'includes' must be a list")
            for inc_data in data["includes"]:
                try:
                    includes.append(validate_include_ref(inc_data))
                except ValueError as e:
                    return error_response("invalid_input", str(e))

    inherits_active = data.get("inherits_active", False)
    if not isinstance(inherits_active, bool):
        return error_response("invalid_input", "'inherits_active' must be boolean")

    if "author" not in data:
        return error_response("invalid_input", "Missing 'author'")

    author = data["author"]
    if not isinstance(author, str):
        return error_response("invalid_input", "'author' must be a string")

    title = data.get("title")
    if title is not None and not isinstance(title, str):
        return error_response("invalid_input", "'title' must be a string")

    description = data.get("description")
    if description is not None and not isinstance(description, str):
        return error_response("invalid_input", "'description' must be a string")

    labels = tuple(sorted(data.get("labels", [])))
    for label in labels:
        if not isinstance(label, str):
            return error_response("invalid_input", "Labels must be strings")

    quorum_req = data.get("quorum", {})
    if not isinstance(quorum_req, dict):
        return error_response("invalid_input", "'quorum' must be an object")

    # Handle inherits_active
    if inherits_active:
        active_config = storage.get_active(name, scope)
        if active_config is not None:
            merged_config = deep_merge(active_config.config, config_data)
            merged_includes = list(active_config.includes) + includes
            config_data = merged_config
            includes = merged_includes

    # Create the draft version
    try:
        version, is_active = storage.create_version(name, scope, config_data, includes)
    except ValueError as e:
        if "Max versions exceeded" in str(e):
            return error_response("conflict", str(e))
        return error_response("internal", str(e))

    # Get base version (the active before this proposal)
    base_version = storage.get_active_version(name, scope)
    if base_version is None:
        base_version = version - 1  # First version, no previous

    # Get configs for diff computation
    draft_config = storage.get_version(name, scope, version)
    base_config = storage.get_version(name, scope, base_version) if base_version else None

    base_resolved = {}
    draft_resolved = {}
    resolution_graph = []

    if draft_config:
        ctx = ResolutionContext()
        try:
            draft_resolved, resolution_graph = resolve_config(
                storage, name, scope, version, ctx
            )
        except ValueError as e:
            pass

    if base_config:
        ctx = ResolutionContext()
        try:
            base_resolved, _ = resolve_config(
                storage, name, scope, base_version, ctx
            )
        except ValueError as e:
            pass

    # Compute diffs
    diffs = compute_diff(base_config.config if base_config else {},
                         draft_config.config if draft_config else {})
    resolved_diffs = compute_resolution_diff(base_resolved, draft_resolved)

    # Run policy evaluation AFTER schema validation (but before creating proposal)
    policy_result, graph_truncated = run_policy_on_propose(name, scope, version)

    # Check for error-severity violations
    error_violations = [v for v in policy_result.violations if v["severity"] == "error"]

    if error_violations:
        # Reject with 422 policy_violation
        details = {
            "policy_summary": policy_result.to_dict(),
            "violations": error_violations
        }
        return error_response("policy_violation",
                            f"Policy evaluation failed with {len(error_violations)} error(s)",
                            details)

    # Create proposal
    quorum = compute_quorum(quorum_req, scope)
    tally = Tally()

    with proposals_lock:
        proposal_id = next_proposal_id
        next_proposal_id += 1

        proposal = ProposalData(
            proposal_id=proposal_id,
            name=name,
            scope=scope,
            draft_version=version,
            base_version=base_version,
            author=author,
            title=title,
            description=description,
            labels=labels,
            quorum=quorum,
            status=ProposalStatus.OPEN,
            tally=tally,
            diffs=DiffArtifacts(
                raw_json_patch=diffs.raw_json_patch,
                resolved_json_patch=resolved_diffs,
                includes_changes=diffs.includes_changes,
                human=diffs.human
            ),
            reviews=[],
            policy_summary=policy_result.to_dict()
        )
        proposals[proposal_id] = proposal

    response = proposal.to_dict()
    return Response(
        canonical_json(response),
        mimetype="application/json; charset=utf-8"
    ), 201


@app.route('/v1/proposals/<int:proposal_id>', methods=['GET'])
def get_proposal(proposal_id: int):
    """Get a proposal by ID."""
    with proposals_lock:
        proposal = proposals.get(proposal_id)

    if proposal is None:
        return error_response("not_found", f"Proposal {proposal_id} not found")

    return Response(
        canonical_json(proposal.to_dict()),
        mimetype="application/json; charset=utf-8"
    ), 200


@app.route('/v1/proposals/<int:proposal_id>:merge', methods=['POST'])
def merge_proposal(proposal_id: int):
    """Merge an approved proposal with final policy check."""
    global proposals, storage

    data = get_json_request()
    if isinstance(data, tuple):
        return data

    with proposals_lock:
        proposal = proposals.get(proposal_id)

    if proposal is None:
        return error_response("not_found", f"Proposal {proposal_id} not found")

    if proposal.status != ProposalStatus.APPROVED:
        return error_response("conflict",
                            f"Proposal {proposal_id} is not in approved state")

    # Re-run policy evaluation just before activation with latest graph
    policy_result, graph_truncated = run_policy_on_propose(
        proposal.name, proposal.scope, proposal.draft_version
    )

    # Check for error-severity violations
    error_violations = [v for v in policy_result.violations if v["severity"] == "error"]

    if error_violations:
        # Block merge with 409 not_mergeable
        details = {
            "policy_summary": policy_result.to_dict(),
            "violations": error_violations
        }
        return error_response("conflict",
                            f"Policy evaluation failed with {len(error_violations)} error(s) - merge blocked",
                            details)

    # Activate the draft version
    storage.activate_version(proposal.name, proposal.scope, proposal.draft_version)

    # Update proposal status
    with proposals_lock:
        proposal = proposals[proposal_id]
        # Create a new proposal object with updated status
        proposals[proposal_id] = ProposalData(
            proposal_id=proposal.proposal_id,
            name=proposal.name,
            scope=proposal.scope,
            draft_version=proposal.draft_version,
            base_version=proposal.base_version,
            author=proposal.author,
            title=proposal.title,
            description=proposal.description,
            labels=proposal.labels,
            quorum=proposal.quorum,
            status=ProposalStatus.MERGED,
            tally=proposal.tally,
            diffs=proposal.diffs,
            reviews=proposal.reviews,
            policy_summary=proposal.policy_summary
        )

    response = {
        "proposal_id": proposal_id,
        "name": proposal.name,
        "scope": proposal.scope.to_dict(),
        "version": proposal.draft_version,
        "active": True
    }
    return Response(
        canonical_json(response),
        mimetype="application/json; charset=utf-8"
    ), 200


@app.route('/v1/proposals/<int:proposal_id>:approve', methods=['POST'])
def approve_proposal(proposal_id: int):
    """Approve a proposal."""
    data = get_json_request()
    if isinstance(data, tuple):
        return data

    if "actor" not in data:
        return error_response("invalid_input", "Missing 'actor'")

    actor = data["actor"]
    if not isinstance(actor, str):
        return error_response("invalid_input", "'actor' must be a string")

    message = data.get("message")

    with proposals_lock:
        proposal = proposals.get(proposal_id)
        if proposal is None:
            return error_response("not_found", f"Proposal {proposal_id} not found")

        if proposal.status != ProposalStatus.OPEN:
            return error_response("conflict",
                                f"Cannot review proposal in {proposal.status.value} state")

        # Check quorum policy
        if not proposal.quorum.allow_author_approval and proposal.author == actor:
            return error_response("invalid_input",
                                "Author not allowed to approve their own proposal")

        # Check if actor is in allowed reviewers (if restricted)
        if proposal.quorum.allowed_reviewers is not None:
            if actor not in proposal.quorum.allowed_reviewers:
                return error_response("invalid_input",
                                    f"Actor '{actor}' not in allowed reviewers list")

        # Add review
        new_tally = proposal.tally.with_decision(actor, 'approve')
        new_reviews = list(proposal.reviews)
        new_reviews.append(Review(actor=actor, decision='approve', message=message))

        # Create updated proposal
        proposals[proposal_id] = ProposalData(
            proposal_id=proposal.proposal_id,
            name=proposal.name,
            scope=proposal.scope,
            draft_version=proposal.draft_version,
            base_version=proposal.base_version,
            author=proposal.author,
            title=proposal.title,
            description=proposal.description,
            labels=proposal.labels,
            quorum=proposal.quorum,
            status=proposal.status,
            tally=new_tally,
            diffs=proposal.diffs,
            reviews=tuple(new_reviews),
            policy_summary=proposal.policy_summary
        )

        # Check if quorum met
        updated_proposal = proposals[proposal_id]
        if updated_proposal.tally.approvals >= updated_proposal.quorum.required_approvals:
            proposals[proposal_id] = ProposalData(
                proposal_id=updated_proposal.proposal_id,
                name=updated_proposal.name,
                scope=updated_proposal.scope,
                draft_version=updated_proposal.draft_version,
                base_version=updated_proposal.base_version,
                author=updated_proposal.author,
                title=updated_proposal.title,
                description=updated_proposal.description,
                labels=updated_proposal.labels,
                quorum=updated_proposal.quorum,
                status=ProposalStatus.APPROVED,
                tally=updated_proposal.tally,
                diffs=updated_proposal.diffs,
                reviews=updated_proposal.reviews,
                policy_summary=updated_proposal.policy_summary
            )

    return get_proposal(proposal_id)


@app.route('/v1/proposals/<int:proposal_id>:reject', methods=['POST'])
def reject_proposal(proposal_id: int):
    """Reject a proposal."""
    data = get_json_request()
    if isinstance(data, tuple):
        return data

    if "actor" not in data:
        return error_response("invalid_input", "Missing 'actor'")

    actor = data["actor"]
    message = data.get("message")

    with proposals_lock:
        proposal = proposals.get(proposal_id)
        if proposal is None:
            return error_response("not_found", f"Proposal {proposal_id} not found")

        if proposal.status != ProposalStatus.OPEN:
            return error_response("conflict",
                                f"Cannot review proposal in {proposal.status.value} state")

        # Add review
        new_tally = proposal.tally.with_decision(actor, 'reject')
        new_reviews = list(proposal.reviews)
        new_reviews.append(Review(actor=actor, decision='reject', message=message))

        # Create updated proposal with rejected status
        proposals[proposal_id] = ProposalData(
            proposal_id=proposal.proposal_id,
            name=proposal.name,
            scope=proposal.scope,
            draft_version=proposal.draft_version,
            base_version=proposal.base_version,
            author=proposal.author,
            title=proposal.title,
            description=proposal.description,
            labels=proposal.labels,
            quorum=proposal.quorum,
            status=ProposalStatus.REJECTED,
            tally=new_tally,
            diffs=proposal.diffs,
            reviews=tuple(new_reviews),
            policy_summary=proposal.policy_summary
        )

    return get_proposal(proposal_id)


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description='Config Management Service')
    parser.add_argument('--address', default='0.0.0.0', help='Address to bind to')
    parser.add_argument('--port', type=int, default=8080, help='Port to listen on')
    args = parser.parse_args()

    app.run(host=args.address, port=args.port, threaded=True)


if __name__ == '__main__':
    main()