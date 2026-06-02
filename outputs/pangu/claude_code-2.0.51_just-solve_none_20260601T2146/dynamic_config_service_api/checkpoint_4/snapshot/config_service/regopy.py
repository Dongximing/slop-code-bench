#!/usr/bin/env python3
"""
Pure Python Rego interpreter for policy evaluation.
This implements a subset of Rego that supports the guardrails pattern
with deny/warn rules returning structured violations.
"""

import ast
import json
import re
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field
from functools import lru_cache


class RegoError(Exception):
    """Error during Rego compilation or evaluation."""
    pass


@dataclass
class Violation:
    """A violation from a rule evaluation."""
    rule_id: str
    message: str
    path: str
    target: str
    severity: str  # "error" or "warn"
    evidence: dict[str, Any] = field(default_factory=dict)
    # Additional fields from the Rego result
    _raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self, policy_name: str, policy_version: int) -> dict[str, Any]:
        """Convert to the standard violation format."""
        result = {
            "policy": {"name": policy_name, "version": policy_version},
            "target": {"name": self.target},
            "rule_id": self.rule_id,
            "severity": self.severity,
            "path": self.path,
            "message": self.message,
        }
        if self.evidence:
            result["evidence"] = self.evidence
        return result


@dataclass
class RuleResult:
    """Result from a single rule evaluation."""
    violations: List[Violation] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


class RegoEngine:
    """A Rego policy engine that evaluates Rego modules."""

    def __init__(self):
        self._rules: Dict[Tuple[str, str], List[dict]] = {}  # (package, rule_name) -> rule definitions
        self._data: Dict[str, Any] = {}
        self._functions: Dict[str, Callable] = {}

        # Register built-in functions
        self._register_builtins()

    def _register_builtins(self):
        """Register built-in Rego functions."""
        self._functions['plus'] = lambda args: args[0] + args[1] if len(args) == 2 else None
        self._functions['minus'] = lambda args: args[0] - args[1] if len(args) == 2 else None
        self._functions['multiply'] = lambda args: args[0] * args[1] if len(args) == 2 else None
        self._functions['equals'] = lambda args: args[0] == args[1] if len(args) == 2 else False
        self._functions['not_equals'] = lambda args: args[0] != args[1] if len(args) == 2 else True
        self._functions['less_than'] = lambda args: args[0] < args[1] if len(args) == 2 else False
        self._functions['greater_than'] = lambda args: args[0] > args[1] if len(args) == 2 else False
        self._functions['less_than_or_equals'] = lambda args: args[0] <= args[1] if len(args) == 2 else False
        self._functions['greater_than_or_equals'] = lambda args: args[0] >= args[1] if len(args) == 2 else False
        self._functions['contains'] = lambda args: args[1] in str(args[0]) if len(args) == 2 else False
        self._functions['startswith'] = lambda args: str(args[0]).startswith(str(args[1])) if len(args) == 2 else False
        self._functions['endswith'] = lambda args: str(args[0]).endswith(str(args[1])) if len(args) == 2 else False
        self._functions['split'] = lambda args: str(args[0]).split(args[1]) if len(args) == 2 else None
        self._functions['lower'] = lambda args: str(args[0]).lower() if len(args) == 1 else None
        self._functions['upper'] = lambda args: str(args[0]).upper() if len(args) == 1 else None
        self._functions['count'] = lambda args: len(args[0]) if len(args) == 1 else None
        self._functions['is_array'] = lambda args: isinstance(args[0], list) if len(args) == 1 else False
        self._functions['is_object'] = lambda args: isinstance(args[0], dict) if len(args) == 1 else False
        self._functions['is_string'] = lambda args: isinstance(args[0], str) if len(args) == 1 else False
        self._functions['is_number'] = lambda args: isinstance(args[0], (int, float)) if len(args) == 1 else False

    def load_bundle(
        self,
        modules: Dict[str, str],
        data: Optional[Dict[str, Any]] = None
    ) -> None:
        """Load Rego modules and data into the engine."""
        for module_name, module_content in modules.items():
            self._parse_module(module_name, module_content)

        if data:
            self._load_data(data)

    def _parse_module(self, module_name: str, content: str) -> None:
        """Parse a Rego module and extract rule definitions."""
        try:
            # Remove comments
            lines = content.split('\n')
            clean_lines = []
            for line in lines:
                line = re.sub(r'#.*$', '', line)
                clean_lines.append(line)
            content = '\n'.join(clean_lines)

            # Parse the Rego
            self._parse_rego(content)
        except Exception as e:
            raise RegoError(f"Error parsing module {module_name}: {e}")

    def _parse_rego(self, content: str) -> None:
        """Parse Rego content and extract rules."""
        # Find package declaration
        package_match = re.search(r'package\s+(\S+)', content)
        if not package_match:
            raise RegoError("Module must have a package declaration")
        package = package_match.group(1)

        # Find all rule definitions
        # Rule pattern: resource_type[head] { body }
        # where resource_type is deny, warn, or other
        rule_pattern = r'(\w+)\s*\[([^\]]+)\]\s*\{([^}]+)\}'

        for match in re.finditer(rule_pattern, content):
            resource_type = match.group(1)
            head = match.group(2).strip()
            body = match.group(3).strip()

            # Store the rule
            if resource_type in ('deny', 'warn'):
                rule_key = (package, f"{resource_type}_{head}")
                self._rules[(package, resource_type)].append({
                    'head': head,
                    'body': body,
                    'resource_type': resource_type,
                    'raw': match.group(0)
                })

    def _load_data(self, data: Dict[str, Any]) -> None:
        """Load data into the engine."""
        self._data.update(data)

    def evaluate(
        self,
        input_data: Dict[str, Any]
    ) -> Tuple[List[Violation], List[str]]:
        """
        Evaluate all rules against the input data.
        Returns a tuple of (violations, errors).
        """
        violations = []
        errors = []

        # Evaluate each rule
        for (package, resource_type), rules in self._rules.items():
            for rule in rules:
                try:
                    result = self._evaluate_rule(rule, input_data)
                    if result:
                        violations.extend(result)
                except Exception as e:
                    errors.append(f"Error evaluating rule {package}.{rule['head']}: {e}")

        return violations, errors

    def _evaluate_rule(
        self,
        rule: dict,
        input_data: Dict[str, Any]
    ) -> Optional[List[Violation]]:
        """Evaluate a single rule and return violations if any."""
        # Parse the rule body
        body = rule['body']
        resource_type = rule['resource_type']

        # Check if the body condition is true
        # Simple condition check
        if self._evaluate_condition(body, input_data):
            # Extract violation details from the head
            head_parts = [p.strip() for p in rule['head'].split(',')]

            violation = self._build_violation(head_parts, input_data, resource_type)
            return [violation]

        return None

    def _evaluate_condition(self, condition: str, input_data: Dict[str, Any]) -> bool:
        """Evaluate a condition expression."""
        # Very simplified condition evaluation
        # This handles basic cases like: input.target.scope.env == "prod"

        # Remove whitespace but preserve string content
        condition = condition.strip()

        try:
            # Try to evaluate as a Python expression
            # This is a security risk - we need to sandbox it
            # For now, we'll do a simplified check

            # Check for "not"
            if condition.startswith('not '):
                sub_cond = condition[4:].strip()
                return not self._evaluate_condition(sub_cond, input_data)

            # Check for "and"
            if ' and ' in condition:
                parts = self._split_and(condition)
                return all(self._evaluate_condition(p.strip(), input_data) for p in parts)

            # Check for "or"
            if ' or ' in condition:
                parts = self._split_or(condition)
                return any(self._evaluate_condition(p.strip(), input_data) for p in parts)

            # Single condition - parse and evaluate
            return self._evaluate_single_condition(condition, input_data)

        except Exception:
            return False

    def _split_and(self, condition: str) -> List[str]:
        """Split a condition by 'and', respecting parentheses."""
        parts = []
        current = ""
        depth = 0

        i = 0
        while i < len(condition):
            if condition[i:i+4] == ' and':
                if depth == 0:
                    parts.append(current.strip())
                    current = ""
                    i += 4  # Skip ' and'
                else:
                    current += condition[i]
                    i += 1
            else:
                if condition[i] == '(':
                    depth += 1
                elif condition[i] == ')':
                    depth -= 1
                current += condition[i]
                i += 1

        if current.strip():
            parts.append(current.strip())

        return parts

    def _split_or(self, condition: str) -> List[str]:
        """Split a condition by 'or', respecting parentheses."""
        parts = []
        current = ""
        depth = 0

        i = 0
        while i < len(condition):
            if condition[i:i+3] == ' or':
                if depth == 0 and (i == 0 or condition[i-1] != '='):
                    parts.append(current.strip())
                    current = ""
                    i += 3  # Skip ' or'
                else:
                    current += condition[i]
                    i += 1
            else:
                if condition[i] == '(':
                    depth += 1
                elif condition[i] == ')':
                    depth -= 1
                current += condition[i]
                i += 1

        if current.strip():
            parts.append(current.strip())

        return parts

    def _evaluate_single_condition(self, condition: str, input_data: Dict[str, Any]) -> bool:
        """Evaluate a single condition."""
        # Pattern: left OP right
        # where left is like input.target.scope.env
        # and right is like "prod" or 3 or true/false

        # Handle negation at the start
        negated = False
        if condition.startswith('not '):
            negated = True
            condition = condition[4:].strip()

        # Try different operators
        for op in ['==', '!=', '<=', '>=', '<', '>']:
            parts = condition.split(op, 1)
            if len(parts) == 2:
                left = parts[0].strip()
                right = parts[1].strip()

                left_val = self._get_value(left, input_data)
                right_val = self._parse_value(right)

                result = self._compare(left_val, right_val, op)
                return not result if negated else result

        # Check if it's a simple truthy condition (e.g., just checking existence)
        # e.g., input.target.resolved_config.db.tls
        try:
            val = self._get_value(condition, input_data)
            result = val is not None and val != False and val != 0 and val != ""
            return not result if negated else result
        except Exception:
            return negated  # If we can't evaluate, it's false (or true if negated)

    def _get_value(self, path: str, data: Dict[str, Any]) -> Any:
        """Get a value from the data using a dot-separated path."""
        # Handle array indexing like foo[0]
        # Remove array indices from path
        path = re.sub(r'\[\d+\]', '', path)
        parts = path.split('.')

        current = data
        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return None

        return current

    def _parse_value(self, value_str: str) -> Any:
        """Parse a value string to its Python equivalent."""
        value_str = value_str.strip()

        # Try to parse as JSON (handles strings, numbers, booleans, null)
        try:
            return json.loads(value_str)
        except json.JSONDecodeError:
            pass

        # Handle unquoted values
        lower_val = value_str.lower()
        if lower_val == 'true':
            return True
        if lower_val == 'false':
            return False
        if lower_val == 'null':
            return None

        # Try to parse as number
        try:
            if '.' in value_str:
                return float(value_str)
            return int(value_str)
        except ValueError:
            pass

        # Return as string (without quotes that were already parsed)
        return value_str

    def _compare(self, left: Any, right: Any, op: str) -> bool:
        """Compare two values."""
        if op == '==':
            return left == right
        elif op == '!=':
            return left != right
        elif op == '<':
            try:
                return left < right
            except TypeError:
                return False
        elif op == '>':
            try:
                return left > right
            except TypeError:
                return False
        elif op == '<=':
            try:
                return left <= right
            except TypeError:
                return False
        elif op == '>=':
            try:
                return left >= right
            except TypeError:
                return False
        return False

    def _build_violation(
        self,
        head_parts: List[str],
        input_data: Dict[str, Any],
        severity: str
    ) -> Violation:
        """Build a violation from rule head parts."""
        # Parse the head which contains the violation object fields
        # Pattern: violation := {"id": "...", "message": "...", "path": "...", ...}

        target_name = input_data.get('target', {}).get('name', 'unknown')

        # Try to extract fields from the head
        rule_id = "unknown_rule"
        message = f"Policy {severity} violated"
        path = ""
        evidence = {}

        for part in head_parts:
            part = part.strip()

            # Look for field assignments in the violation object
            # Simplified parsing - look for key-value pairs
            if '"id"' in part or "'id'" in part:
                match = re.search(r'[\"\']id[\"\']\s*:\s*[\"\']([^\"\']+)[\"\']', part)
                if match:
                    rule_id = match.group(1)

            if '"message"' in part or "'message'" in part:
                match = re.search(r'[\"\']message[\"\']\s*:\s*[\"\']([^\"\']+)[\"\']', part)
                if match:
                    message = match.group(1)

            if '"path"' in part or "'path'" in part:
                match = re.search(r'[\"\']path[\"\']\s*:\s*[\"\']([^\"\']+)[\"\']', part)
                if match:
                    path = match.group(1)

            # Check if this part references input data
            if 'input.target.name' in part:
                match = re.search(r'input\.target\.name\s*[:=]\s*([a-zA-Z0-9_]+)', part)
                if match:
                    target_name = match.group(1)

        return Violation(
            rule_id=rule_id,
            message=message,
            path=path,
            target=target_name,
            severity='error' if severity == 'deny' else 'warn',
            evidence=evidence
        )


class SimpleRegoEngine:
    """
    A simple Rego-like rule evaluator for policy checks.
    Supports the guardrails pattern with deny/warn rules.
    """

    def __init__(self):
        self._rules: List[dict] = []
        self._data: Dict[str, Any] = {}

    def load_bundle(
        self,
        modules: Dict[str, str],
        data: Optional[Dict[str, Any]] = None
    ) -> None:
        """Load Rego modules and data into the engine."""
        for module_name, module_content in modules.items():
            self._parse_module(module_content)

        if data:
            self._data.update(data)

    def _parse_module(self, content: str) -> None:
        """Parse a Rego module and extract rules."""
        # Remove comments
        lines = []
        for line in content.split('\n'):
            # Strip inline comments
            if '#' in line:
                line = line[:line.index('#')]
            lines.append(line)
        content = '\n'.join(lines)

        # Find package
        package_match = re.search(r'package\s+(\S+)', content)
        if not package_match:
            raise ValueError("Rego module must declare a package")
        package = package_match.group(1)

        # Extract deny rules
        deny_pattern = r'deny\[([^\]]+)\]\s*\{([^}]+)\}'
        for match in re.finditer(deny_pattern, content, re.DOTALL):
            self._rules.append({
                'type': 'deny',
                'expression': match.group(1).strip(),
                'body': match.group(2).strip(),
                'package': package
            })

        # Extract warn rules
        warn_pattern = r'warn\[([^\]]+)\]\s*\{([^}]+)\}'
        for match in re.finditer(warn_pattern, content, re.DOTALL):
            self._rules.append({
                'type': 'warn',
                'expression': match.group(1).strip(),
                'body': match.group(2).strip(),
                'package': package
            })

    def evaluate(
        self,
        target: dict,
        graph: dict,
        now: str
    ) -> Tuple[List[dict], List[dict]]:
        """
        Evaluate rules against the evaluation context.
        Returns (violations, errors) where violations are the standard format.
        """
        violations = []
        errors = []

        context = {
            'target': target,
            'graph': graph,
            'now': now,
            'input': {
                'target': target,
                'graph': graph
            }
        }

        for rule in self._rules:
            try:
                result = self._evaluate_rule(rule, context)
                if result:
                    violations.append(result)
            except Exception as e:
                errors.append({
                    'rule': f"{rule['package']}.{rule['type']}",
                    'error': str(e)
                })

        return violations, errors

    def _evaluate_rule(self, rule: dict, context: dict) -> Optional[dict]:
        """Evaluate a single rule and return a violation if triggered."""
        body = rule['body']

        # Check if the body evaluates to true
        if not self._evaluate_expression(body, context):
            return None

        # Extract the violation details from the rule head
        head = rule['expression']

        # Parse the violation object
        violation = self._parse_violation(head, context, rule['type'])
        return violation

    def _evaluate_expression(self, expr: str, context: dict) -> bool:
        """Evaluate a Rego expression in the given context."""
        # Remove whitespace
        expr = expr.strip()

        # Handle negation
        if expr.startswith('not '):
            return not self._evaluate_expression(expr[4:], context)

        # Handle boolean literals
        if expr.lower() == 'true':
            return True
        if expr.lower() == 'false':
            return False

        # Handle 'with' statements (simplified)
        # with input as ... { ... }
        if 'with' in expr:
            # Extract the with clause
            main_expr = expr.split('with')[0].strip()
            if not main_expr:
                main_expr = 'true'
            # Simplified: we ignore 'with' and just evaluate the body
            return self._evaluate_expression(main_expr, context)

        # Check for and/or operators
        if ' and ' in expr:
            parts = self._split_by_operator(expr, ' and ')
            return all(self._evaluate_expression(p, context) for p in parts)

        if ' or ' in expr:
            parts = self._split_by_operator(expr, ' or ')
            return any(self._evaluate_expression(p, context) for p in parts)

        # Single expression: either a comparison or a path reference
        # Comparison patterns: left op right
        for op in ['==', '!=', '<=', '>=', '<', '>']:
            if op in expr:
                left, right = expr.split(op, 1)
                left_val = self._eval_path(left.strip(), context)
                right_val = self._parse_literal(right.strip())
                return self._compare_values(left_val, right_val, op)

        # It's a path reference - check if it's truthy
        val = self._eval_path(expr, context)
        return val is not None and val is not False

    def _split_by_operator(self, expr: str, op: str) -> List[str]:
        """Split expression by operator, respecting parentheses."""
        parts = []
        current = ""
        depth = 0

        i = 0
        while i < len(expr):
            if len(op) > 1:
                # Check for operator at current position
                if expr[i:i+len(op)] == op:
                    if depth == 0:
                        parts.append(current.strip())
                        current = ""
                        i += len(op)
                        continue
            else:
                if expr[i] == op[0]:
                    if depth == 0:
                        parts.append(current.strip())
                        current = ""
                        i += 1
                        continue

            # Track parentheses
            if expr[i] == '(':
                depth += 1
            elif expr[i] == ')':
                depth -= 1

            current += expr[i]
            i += 1

        if current.strip():
            parts.append(current.strip())

        return parts

    def _eval_path(self, path: str, context: dict) -> Any:
        """Evaluate a path expression."""
        # Handle array slices (simplified)
        # foo[_] -> returns all values
        # foo[n] -> returns n-th value

        # Remove brackets
        path = re.sub(r'\[(\d+)\]', r'.\1', path)
        path = re.sub(r'\[_\]', '', path)

        parts = path.strip('.').split('.')
        current = context

        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
            elif isinstance(current, list):
                try:
                    idx = int(part)
                    if 0 <= idx < len(current):
                        current = current[idx]
                    else:
                        return None
                except ValueError:
                    return None
            else:
                return None

        return current

    def _parse_literal(self, literal: str) -> Any:
        """Parse a literal value."""
        literal = literal.strip()

        # Try to parse as JSON
        try:
            return json.loads(literal)
        except json.JSONDecodeError:
            pass

        # Boolean
        if literal.lower() == 'true':
            return True
        if literal.lower() == 'false':
            return False

        # Number
        try:
            if '.' in literal:
                return float(literal)
            return int(literal)
        except ValueError:
            pass

        # None/null
        if literal.lower() == 'null':
            return None

        # String (remove quotes if present)
        if (literal.startswith('"') and literal.endswith('"')) or \
           (literal.startswith("'") and literal.endswith("'")):
            return literal[1:-1]

        return literal

    def _compare_values(self, left: Any, right: Any, op: str) -> bool:
        """Compare two values."""
        if op == '==':
            return left == right
        elif op == '!=':
            return left != right
        elif op == '<':
            try:
                return left < right
            except TypeError:
                return False
        elif op == '>':
            try:
                return left > right
            except TypeError:
                return False
        elif op == '<=':
            try:
                return left <= right
            except TypeError:
                return False
        elif op == '>=':
            try:
                return left >= right
            except TypeError:
                return False
        return False

    def _parse_violation(self, head: str, context: dict, severity: str) -> dict:
        """Parse a violation expression."""
        # Pattern: ")": {"id": "...", "message": "...", "path": "...", ...}
        # or just some assignment

        violation = {
            'policy': {'name': 'unknown', 'version': 0},
            'target': {'name': 'unknown'},
            'rule_id': 'unknown',
            'severity': 'error' if severity == 'deny' else 'warn',
            'path': '',
            'message': 'Policy violation',
            'evidence': {}
        }

        # Extract key fields from the head
        # Try JSON object format
        if '{' in head and '}' in head:
            start = head.index('{')
            end = head.rindex('}')
            try:
                obj_str = head[start:end+1]
                obj = json.loads(obj_str)

                if 'id' in obj:
                    violation['rule_id'] = obj['id']
                if 'msg' in obj:
                    violation['message'] = obj['msg']
                elif 'message' in obj:
                    violation['message'] = obj['message']
                if 'path' in obj:
                    violation['path'] = obj['path']
                if 'target' in obj:
                    if isinstance(obj['target'], str):
                        violation['target']['name'] = obj['target']
                    elif isinstance(obj['target'], dict):
                        violation['target'] = obj['target']
                if 'severity' in obj:
                    violation['severity'] = obj['severity']
                if 'evidence' in obj:
                    violation['evidence'] = obj['evidence']
            except json.JSONDecodeError:
                pass

        return violation
