#!/usr/bin/env python3
"""Policy evaluation engine using pure Python. Supports Rego rules with deny/warn patterns."""

import json
import re
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

import time

from models import (
    PolicyBundle,
    PolicyBinding,
    PolicyStackEntry,
    PolicyViolation,
    PolicyEvaluationResult,
    ConfigError,
    ResolutionNode
)

from regopy import SimpleRegoEngine


# Maximums from spec
MAX_EVALUATION_TIME_MS = 500
MAX_VIOLATIONS = 1000
MAX_GRAPH_SIZE = 2000

# Global lock for engine
_engine_lock = threading.Lock()


class PolicyEngine:
    """Policy evaluation engine that evaluates Rego rules against configs."""

    def __init__(self):
        # Cache compiled engines per bundle
        self._engines: Dict[Tuple[str, int], SimpleRegoEngine] = {}
        self._lock = threading.Lock()

    def load_policy_bundle(self, bundle: PolicyBundle) -> None:
        """Load a policy bundle into the engine.

        Args:
            bundle: The policy bundle to load.

        Raises:
            ConfigError: If the bundle cannot be parsed or is invalid.
        """
        with self._lock:
            key = (bundle.name, bundle.version)

            try:
                engine = SimpleRegoEngine()
                engine.load_bundle(bundle.rego_modules, bundle.data)
                self._engines[key] = engine
            except Exception as e:
                raise ConfigError('policy_invalid',
                    f'Failed to parse policy bundle: {e}',
                    {'details': str(e)})

    def unload_policy_bundle(self, name: str, version: int) -> None:
        """Remove a policy bundle from the engine."""
        with self._lock:
            self._engines.pop((name, version), None)

    def evaluate(
        self,
        target_name: str,
        target_scope: Dict[str, str],
        target_version: int,
        resolved_config: Dict[str, Any],
        graph_nodes: List[ResolutionNode],
        bindings: List[PolicyBinding],
        now: Optional[str] = None
    ) -> PolicyEvaluationResult:
        """Evaluate policies against a target config.

        Args:
            target_name: The name of the target config.
            target_scope: The scope of the target config.
            target_version: The version of the target config.
            resolved_config: The resolved config to evaluate.
            graph_nodes: Nodes in the resolution graph.
            bindings: Policy bindings to apply.
            now: Optional timestamp in RFC3339 format.

        Returns:
            PolicyEvaluationResult with stack, violations, and tallies.

        Raises:
            ConfigError: If evaluation times out or encounters an error.
        """
        if now is None:
            now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

        target = {
            'name': target_name,
            'scope': target_scope,
            'version_used': target_version,
            'resolved_config': resolved_config
        }

        # Build graph by name
        graph = {
            'nodes': [],
            'by_name': {}
        }

        # Sort graph nodes by name ascending
        sorted_nodes = sorted(graph_nodes, key=lambda n: n.name)
        for node in sorted_nodes:
            node_dict = node.to_dict()
            graph['nodes'].append(node_dict)

        # Check graph size
        graph_truncated = False
        if len(graph['nodes']) > MAX_GRAPH_SIZE:
            graph_truncated = True
            graph['nodes'] = graph['nodes'][:MAX_GRAPH_SIZE]

        # Rebuild by_name after truncation
        graph['by_name'] = {}
        for node_dict in graph['nodes']:
            graph['by_name'][node_dict['name']] = node_dict

        # Build policy stack from bindings
        policy_stack = []
        for binding in bindings:
            stack_entry = PolicyStackEntry(
                bundle=binding.bundle,
                selector=binding.selector,
                graph_keys=binding.graph_keys,
                priority=binding.priority
            )
            policy_stack.append(stack_entry)

        # Sort stack by priority descending, then bundle name, then version
        policy_stack.sort(key=lambda e: (-e.priority, e.bundle['name'], e.bundle['version']))

        # Collect engines for each unique bundle
        engines_to_evaluate = []
        seen = set()
        for binding in bindings:
            key = (binding.bundle['name'], binding.bundle['version'])
            if key not in seen:
                seen.add(key)
                with self._lock:
                    engine = self._engines.get(key)
                    if engine:
                        engines_to_evaluate.append((binding, engine))

        # Evaluate policies with timeout
        all_violations = []

        def evaluate_binding(binding: PolicyBinding, engine: SimpleRegoEngine) -> Tuple[List[dict], str]:
            try:
                violations, errors = engine.evaluate(target, graph, now)
                return violations, ''
            except Exception as e:
                return [], str(e)

        # Evaluate in parallel with timeout
        start_time = time.time()

        try:
            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = []
                for binding, engine in engines_to_evaluate:
                    future = executor.submit(evaluate_binding, binding, engine)
                    futures.append((binding, future))

                for binding, future in futures:
                    try:
                        result = future.result(timeout=(MAX_EVALUATION_TIME_MS / 1000.0))
                        violations, error = result
                        if error:
                            # Log error but continue
                            pass
                        all_violations.extend(violations)
                    except FutureTimeoutError:
                        raise ConfigError('evaluation_timeout',
                            f'Policy engine exceeded the time budget ({MAX_EVALUATION_TIME_MS}ms)')

        except TimeoutError:
            raise ConfigError('evaluation_timeout',
                f'Policy engine exceeded the time budget ({MAX_EVALUATION_TIME_MS}ms)')

        elapsed_ms = (time.time() - start_time) * 1000

        # Check if we exceeded time budget
        if elapsed_ms > MAX_EVALUATION_TIME_MS:
            raise ConfigError('evaluation_timeout',
                f'Policy engine exceeded the time budget ({MAX_EVALUATION_TIME_MS}ms)')

        # Convert violations to standard format
        policy_violations = []

        for v in all_violations:
            # Map severity
            severity = v.get('severity', 'error')
            if severity not in ('error', 'warn'):
                severity = 'error'

            # Find the binding that produced this violation
            # Use the target name from the violation or the evaluation target
            target_name = v.get('target', target_name)
            if isinstance(target_name, dict):
                target_name = target_name.get('name', target_name)

            # Build target object
            if isinstance(target_name, str):
                violation_target = {
                    'name': target_name,
                    'scope': target_scope,
                    'version_used': target_version
                }
            else:
                violation_target = target_name

            # Extract the policy info
            policy_info = v.get('policy', {})
            policy_name = policy_info.get('name', 'unknown')
            policy_version = policy_info.get('version', 0)

            # Determine evidence if available
            evidence = None
            if 'actual' in v and 'expected' in v:
                evidence = {
                    'actual': v['actual'],
                    'expected': v['expected']
                }

            violation = PolicyViolation(
                policy={'name': policy_name, 'version': policy_version},
                target=violation_target,
                rule_id=v.get('rule_id', 'unknown'),
                severity=severity,
                path=v.get('path', ''),
                message=v.get('message', 'Policy violation'),
                evidence=evidence
            )
            policy_violations.append(violation)

        # Truncate if needed
        truncated = False
        if len(policy_violations) > MAX_VIOLATIONS:
            truncated = True
            policy_violations = policy_violations[:MAX_VIOLATIONS]

        # Sort violations lexicographically
        policy_violations.sort(key=lambda v: (
            v.target.get('name', ''),
            v.policy.get('name', ''),
            v.policy.get('version', 0),
            v.rule_id,
            v.path
        ))

        # Build result
        result = PolicyEvaluationResult(
            policy_stack=policy_stack,
            violations=policy_violations,
            truncated=truncated,
            graph_truncated=graph_truncated
        )

        return result

    def explain_violation(
        self,
        bundle_name: str,
        bundle_version: int,
        target_name: str,
        target_scope: Dict[str, str],
        resolved_config: Dict[str, Any],
        rule_id: str,
        path: str
    ) -> List[str]:
        """Generate an explanation for a specific violation.

        Returns a list of human-readable lines explaining why the violation occurred.
        """
        explanation = []

        # Add selector match line
        selector_parts = [f'{k}={v}' for k, v in sorted(target_scope.items())]
        selector_str = ', '.join(selector_parts)
        explanation.append(f'Selector matched: {selector_str}')

        # Add resolved config value line
        if path:
            # Navigate to the value at the path
            parts = path.strip('/').split('/')
            value = resolved_config
            try:
                for part in parts:
                    if isinstance(value, dict):
                        value = value.get(part)
                    else:
                        value = None
                        break
                if isinstance(value, bool):
                    value_str = 'true' if value else 'false'
                elif value is None:
                    value_str = 'null'
                else:
                    value_str = json.dumps(value)
            except Exception:
                value_str = 'unknown'
            explanation.append(f'Resolved {target_name}@{path} = {value_str}')

        # Add rule expectation line
        explanation.append(f'Rule {rule_id} expects specific conditions')

        # Add decision line
        explanation.append('Decision: DENY (error)')

        return explanation

    def close(self):
        """Clean up resources."""
        with self._lock:
            self._engines.clear()


# Global engine instance
_global_engine: Optional[PolicyEngine] = None
_global_lock = threading.Lock()


def get_engine() -> PolicyEngine:
    """Get the global policy engine instance."""
    global _global_engine
    if _global_engine is None:
        with _global_lock:
            if _global_engine is None:
                _global_engine = PolicyEngine()
    return _global_engine


def shutdown():
    """Shutdown the global policy engine."""
    global _global_engine
    with _global_lock:
        if _global_engine:
            _global_engine.close()
            _global_engine = None
