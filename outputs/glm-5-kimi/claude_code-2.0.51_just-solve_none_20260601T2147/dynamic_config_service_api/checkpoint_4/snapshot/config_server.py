#!/usr/bin/env python3
"""
Config Server - A configuration management service with schema registry support,
change-management workflow for configuration activation, and policy guardrails.
"""

import argparse
import base64
import io
import json
import os
import re
import sys
import tarfile
import threading
import time
import traceback
from copy import deepcopy
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs
import jsonschema
from jsonschema import validate
import yaml
import toml


def normalize_value(value: Any) -> Any:
    """Normalize a JSON value to canonical form with sorted keys."""
    if isinstance(value, dict):
        return {k: normalize_value(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [normalize_value(item) for item in value]
    if isinstance(value, float) and value == int(value) and abs(value) < 10**15:
        return float(int(value))
    return value


def canonical_json(obj: Any) -> str:
    """Serialize object to canonical JSON string."""
    return json.dumps(normalize_value(obj), separators=(',', ':'), sort_keys=True)


class ParseError(Exception):
    """Error parsing raw config."""
    def __init__(self, message: str, reason: str = None):
        super().__init__(message)
        self.reason = reason


def parse_json(raw: str) -> Any:
    """Parse a JSON string with strict rules (no comments, no trailing commas)."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ParseError(f"Invalid JSON: {e}")


class SafeYamlLoader(yaml.SafeLoader):
    """Custom YAML loader that disallows anchors, aliases, custom tags, and merge keys."""
    pass


def yaml_construct_undefined(loader, node):
    raise ParseError("YAML custom tags are not allowed", "yaml_feature_not_allowed")


def yaml_construct_merge(loader, node):
    raise ParseError("YAML merge keys are not allowed", "yaml_feature_not_allowed")


SafeYamlLoader.add_constructor(None, yaml_construct_undefined)
SafeYamlLoader.add_constructor('tag:yaml.org,2002:null', lambda l, n: None)
SafeYamlLoader.add_constructor('tag:yaml.org,2002:bool', lambda l, n: l.construct_yaml_bool(n))
SafeYamlLoader.add_constructor('tag:yaml.org,2002:int', lambda l, n: l.construct_yaml_int(n))
SafeYamlLoader.add_constructor('tag:yaml.org,2002:float', lambda l, n: l.construct_yaml_float(n))
SafeYamlLoader.add_constructor('tag:yaml.org,2002:str', lambda l, n: l.construct_yaml_str(n))
SafeYamlLoader.add_constructor('tag:yaml.org,2002:seq', lambda l, n: l.construct_yaml_seq(n))
SafeYamlLoader.add_constructor('tag:yaml.org,2002:map', lambda l, n: l.construct_yaml_map(n))
SafeYamlLoader.add_constructor('tag:yaml.org,2002:binary', lambda l, n: l.construct_yaml_binary(n))
SafeYamlLoader.add_constructor('tag:yaml.org,2002:timestamp', lambda l, n: l.construct_yaml_timestamp(n))
SafeYamlLoader.add_constructor('tag:yaml.org,2002:omap', lambda l, n: l.construct_yaml_omap(n))
SafeYamlLoader.add_constructor('tag:yaml.org,2002:pairs', lambda l, n: l.construct_yaml_pairs(n))
SafeYamlLoader.add_constructor('tag:yaml.org,2002:set', lambda l, n: l.construct_yaml_set(n))


def parse_yaml(raw: str) -> Any:
    """Parse a YAML string with strict rules."""
    try:
        if '<<:' in raw or '<< :' in raw:
            raise ParseError("YAML merge keys are not allowed", "yaml_feature_not_allowed")

        if '&' in raw or '*' in raw:
            lines = raw.split('\n')
            for line in lines:
                stripped = re.sub(r'"[^"]*"', '', line)
                stripped = re.sub(r"'[^']*'", '', stripped)
                if re.search(r'&\w+', stripped):
                    raise ParseError("YAML anchors are not allowed", "yaml_feature_not_allowed")
                if re.search(r'\*\w+', stripped):
                    raise ParseError("YAML aliases are not allowed", "yaml_feature_not_allowed")

        if re.search(r'!\w+', raw):
            lines = raw.split('\n')
            for line in lines:
                stripped = re.sub(r'"[^"]*"', '', line)
                stripped = re.sub(r"'[^']*'", '', stripped)
                if re.search(r'(?<!!)!\w+', stripped):
                    raise ParseError("YAML custom tags are not allowed", "yaml_feature_not_allowed")

        result = yaml.load(raw, Loader=SafeYamlLoader)

        def check_keys(obj):
            if isinstance(obj, dict):
                for k in obj.keys():
                    if not isinstance(k, str):
                        raise ParseError("YAML mapping keys must be strings", "yaml_feature_not_allowed")
                    check_keys(obj[k])
            elif isinstance(obj, list):
                for item in obj:
                    check_keys(item)

        check_keys(result)
        return result
    except yaml.YAMLError as e:
        raise ParseError(f"Invalid YAML: {e}")


def parse_toml(raw: str) -> Any:
    """Parse a TOML string with strict rules (only JSON-representable values)."""
    try:
        result = toml.loads(raw)

        def check_json_types(obj, path=""):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    check_json_types(v, f"{path}/{k}")
            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    check_json_types(item, f"{path}/{i}")
            elif hasattr(obj, 'isoformat'):
                raise ParseError(f"Non-JSON type at {path}: datetime not allowed", "non_json_type")

        check_json_types(result)
        return result
    except toml.TomlDecodeError as e:
        raise ParseError(f"Invalid TOML: {e}")


def parse_raw_config(raw: str, fmt: str) -> Any:
    fmt = fmt.lower()
    parsers = {'json': parse_json, 'yaml': parse_yaml, 'toml': parse_toml}
    if fmt not in parsers:
        raise ParseError(f"Unsupported format: {fmt}")
    return parsers[fmt](raw)


class SchemaValidationError(Exception):
    """Error during schema validation."""
    def __init__(self, path: str, rule: str, expected: str, actual: str):
        self.path = path
        self.rule = rule
        self.expected = expected
        self.actual = actual
        super().__init__(f"Validation failed at {path}")


def get_json_type(value: Any) -> str:
    """Get the JSON type of a value."""
    if value is None:
        return "null"
    elif isinstance(value, bool):
        return "boolean"
    elif isinstance(value, int):
        return "integer"
    elif isinstance(value, float):
        return "number"
    elif isinstance(value, str):
        return "string"
    elif isinstance(value, list):
        return "array"
    elif isinstance(value, dict):
        return "object"
    return "unknown"


class ExternalRefError(Exception):
    pass


def check_external_refs(obj):
    """Raise ExternalRefError if obj contains any external $ref."""
    if isinstance(obj, dict):
        if '$ref' in obj:
            ref = obj['$ref']
            if ref.startswith(('http://', 'https://', '//')):
                raise ExternalRefError()
            if not ref.startswith('#') and ':' in ref.split('/')[0]:
                raise ExternalRefError()
        for v in obj.values():
            check_external_refs(v)
    elif isinstance(obj, list):
        for item in obj:
            check_external_refs(item)


def _format_validation_error(e) -> SchemaValidationError:
    path = "/" + "/".join(str(p) for p in e.absolute_path) if e.absolute_path else "/"
    rule = e.validator
    val = e.validator_value

    if rule == "type":
        expected = ", ".join(val) if isinstance(val, list) else val
        actual = get_json_type(e.instance)
    elif rule == "enum":
        expected = ", ".join(repr(v) for v in val)
        actual = get_json_type(e.instance)
    elif rule == "required":
        path = path + "/" + val[0] if val else path
        expected = "property required"
        actual = "missing"
    elif rule == "pattern":
        expected = f"matching pattern {val}"
        actual = get_json_type(e.instance)
    elif rule in ("minimum", "maximum"):
        op = ">=" if rule == "minimum" else "<="
        expected = f"{op} {val}"
        actual = str(e.instance)
    elif rule in ("minLength", "maxLength"):
        op = "length >=" if rule == "minLength" else "length <="
        expected = f"{op} {val}"
        actual = f"length {len(e.instance)}"
    elif rule in ("minItems", "maxItems"):
        op = "items >=" if rule == "minItems" else "items <="
        expected = f"{op} {val}"
        actual = f"items {len(e.instance)}"
    else:
        expected = str(val)
        actual = str(e.instance)[:50]

    return SchemaValidationError(path, rule, expected, actual)


def validate_config_against_schema(config: Any, schema: Dict) -> None:
    try:
        check_external_refs(schema)
        validate(instance=config, schema=schema)
    except jsonschema.exceptions.ValidationError as e:
        raise _format_validation_error(e)
    except ExternalRefError:
        raise SchemaValidationError("/", "schema", "no external $ref", "external $ref found")
    except Exception as e:
        raise SchemaValidationError("/", "schema", "valid schema", str(e))


def escape_json_pointer(s: str) -> str:
    """Escape a string for JSON Pointer."""
    return s.replace('~', '~0').replace('/', '~1')


def unescape_json_pointer(s: str) -> str:
    """Unescape a JSON Pointer string."""
    return s.replace('~1', '/').replace('~0', '~')


def get_value_by_pointer(obj: Any, pointer: str) -> Any:
    """Get a value from an object using a JSON Pointer."""
    if pointer == '':
        return obj
    parts = pointer.lstrip('/').split('/')
    current = obj
    for part in parts:
        part = unescape_json_pointer(part)
        if isinstance(current, dict):
            if part not in current:
                raise KeyError(f"Key not found: {part}")
            current = current[part]
        elif isinstance(current, list):
            try:
                idx = int(part)
                current = current[idx]
            except (ValueError, IndexError):
                raise KeyError(f"Index not found: {part}")
        else:
            raise KeyError(f"Cannot traverse non-container at: {part}")
    return current


def compute_json_patch(old: Any, new: Any, path: str = '') -> List[Dict]:
    """Compute RFC 6902 JSON Patch operations from old to new, sorted deterministically."""
    operations = []
    old_normalized = normalize_value(old)
    new_normalized = normalize_value(new)

    if isinstance(old_normalized, dict) and isinstance(new_normalized, dict):
        old_keys = set(old_normalized.keys())
        new_keys = set(new_normalized.keys())

        for key in sorted(old_keys - new_keys):
            operations.append({
                "op": "remove",
                "path": path + '/' + escape_json_pointer(key)
            })

        for key in sorted(new_keys):
            key_path = path + '/' + escape_json_pointer(key)
            if key not in old_keys:
                operations.append({
                    "op": "add",
                    "path": key_path,
                    "value": new_normalized[key]
                })
            elif old_normalized[key] != new_normalized[key]:
                operations.extend(compute_json_patch(old_normalized[key], new_normalized[key], key_path))

    elif isinstance(old_normalized, list) and isinstance(new_normalized, list):
        old_len = len(old_normalized)
        new_len = len(new_normalized)

        if old_normalized == new_normalized:
            return []

        for i in range(new_len, old_len):
            operations.append({
                "op": "remove",
                "path": path + '/' + str(new_len)
            })

        for i in range(new_len):
            elem_path = path + '/' + str(i)
            if i >= old_len:
                operations.append({
                    "op": "add",
                    "path": elem_path,
                    "value": new_normalized[i]
                })
            elif old_normalized[i] != new_normalized[i]:
                operations.extend(compute_json_patch(old_normalized[i], new_normalized[i], elem_path))

    elif old_normalized != new_normalized:
        operations.append({
            "op": "replace",
            "path": path if path else '/',
            "value": new_normalized
        })

    return operations


def sort_patch_operations(ops: List[Dict]) -> List[Dict]:
    """Sort patch operations by path, with remove before replace before add."""
    op_order = {"remove": 0, "replace": 1, "add": 2}

    def sort_key(op):
        path = op.get("path", "")
        return (path, op_order.get(op.get("op", ""), 99))

    return sorted(ops, key=sort_key)


def compute_diffs(old_config: Dict, new_config: Dict,
                  old_resolved: Dict, new_resolved: Dict,
                  old_includes: List[Dict], new_includes: List[Dict]) -> Dict:
    """Compute all diff artifacts between old and new configs."""
    raw_patch = compute_json_patch(old_config, new_config)
    raw_patch = sort_patch_operations(raw_patch)

    resolved_patch = compute_json_patch(old_resolved, new_resolved)
    resolved_patch = sort_patch_operations(resolved_patch)

    includes_changes = compute_includes_diff(old_includes, new_includes)

    human = compute_human_diff(raw_patch, includes_changes)

    return {
        "raw_json_patch": raw_patch,
        "resolved_json_patch": resolved_patch,
        "includes_changes": includes_changes,
        "human": human
    }


def compute_includes_diff(old_includes: List[Dict], new_includes: List[Dict]) -> List[Dict]:
    changes = []

    def normalize_include(inc):
        return {
            "name": inc.get("name"),
            "scope": normalize_value(inc.get("scope", {})),
            "version": inc.get("version")
        }

    old_normalized = [normalize_include(inc) for inc in old_includes]
    new_normalized = [normalize_include(inc) for inc in new_includes]

    old_len = len(old_normalized)
    new_len = len(new_normalized)

    max_len = max(old_len, new_len)

    for i in range(max_len):
        if i >= old_len:
            changes.append({
                "op": "add",
                "index": i,
                "ref": new_includes[i]
            })
        elif i >= new_len:
            changes.append({
                "op": "remove",
                "index": i,
                "ref": old_includes[i]
            })
        elif old_normalized[i] != new_normalized[i]:
            changes.append({
                "op": "update",
                "index": i,
                "from_version": old_normalized[i]["version"],
                "to_version": new_normalized[i]["version"]
            })

    return changes


def compute_human_diff(raw_patch: List[Dict], includes_changes: List[Dict]) -> List[str]:
    human_lines = []

    for op in raw_patch:
        path = op.get("path", "")
        operation = op.get("op")

        if operation == "remove":
            human_lines.append(f"DELETE {path}")
        elif operation == "replace":
            value = canonical_json(op.get("value"))
            human_lines.append(f"REPLACE {path}: {value}")
        elif operation == "add":
            value = canonical_json(op.get("value"))
            human_lines.append(f"SET {path}: {value}")

    for change in includes_changes:
        op_type = change.get("op")
        index = change.get("index")

        if op_type == "add":
            ref = change.get("ref", {})
            name = ref.get("name", "")
            scope_json = canonical_json(ref.get("scope", {}))
            version = ref.get("version")
            human_lines.append(f"INCLUDE_ADD [{index}] {name}@{scope_json} v={canonical_json(version)}")
        elif op_type == "remove":
            ref = change.get("ref", {})
            name = ref.get("name", "")
            scope_json = canonical_json(ref.get("scope", {}))
            version = ref.get("version")
            human_lines.append(f"INCLUDE_REMOVE [{index}] {name}@{scope_json} v={canonical_json(version)}")
        elif op_type == "update":
            human_lines.append(f"INCLUDE_UPDATE [{index}]: {canonical_json(change.get('from_version'))} -> {canonical_json(change.get('to_version'))}")

    _SORT_ORDER = {"DELETE": 0, "REPLACE": 1, "SET": 2, "INCLUDE_ADD": 3,
                   "INCLUDE_REMOVE": 4, "INCLUDE_UPDATE": 5}

    def sort_key(line):
        for prefix, order in _SORT_ORDER.items():
            if line.startswith(prefix):
                return (order, line)
        return (6, line)

    return sorted(human_lines, key=sort_key)


class SchemaRegistry:
    """Registry for JSON schemas with versioning."""

    def __init__(self):
        self.schemas: Dict[str, Dict[int, Dict]] = {}

    def create(self, name: str, schema: Dict) -> int:
        """Create a new schema version, returning the version number."""
        if name not in self.schemas:
            self.schemas[name] = {}

        versions = self.schemas[name]
        if len(versions) >= 1000:
            raise ValueError("Maximum schema versions exceeded")

        new_version = max(versions.keys(), default=0) + 1
        versions[new_version] = deepcopy(schema)
        return new_version

    def get(self, name: str, version: int) -> Optional[Dict]:
        """Get a specific schema version."""
        if name not in self.schemas:
            return None
        return self.schemas[name].get(version)

    def list_versions(self, name: str) -> List[int]:
        """List all versions of a schema."""
        if name not in self.schemas:
            return []
        return sorted(self.schemas[name].keys())

    def exists(self, name: str, version: int) -> bool:
        """Check if a schema version exists."""
        return name in self.schemas and version in self.schemas[name]


class ConfigStore:
    """Store for configurations with versioning and workflow support."""

    def __init__(self):
        self.configs: Dict[Tuple[str, str], Dict[int, Dict]] = {}
        self.bindings: Dict[Tuple[str, str], Dict] = {}
        self.active_versions: Dict[Tuple[str, str], int] = {}
        self.version_status: Dict[Tuple[str, str, int], str] = {}

    @staticmethod
    def scope_to_key(scope: Dict) -> str:
        return json.dumps(sorted(scope.items()))

    @staticmethod
    def key_to_scope(key: str) -> Dict:
        return dict(json.loads(key))

    def _key(self, name: str, scope: Dict) -> Tuple[str, str]:
        return (name, self.scope_to_key(scope))

    def create_version(self, name: str, scope: Dict, config: Dict,
                       includes: List[Dict] = None, schema_ref: Dict = None) -> int:
        key = self._key(name, scope)

        if key not in self.configs:
            self.configs[key] = {}

        versions = self.configs[key]
        new_version = max(versions.keys(), default=0) + 1

        versions[new_version] = {
            "config": normalize_value(deepcopy(config)),
            "includes": includes or [],
            "schema_ref": schema_ref
        }

        self.version_status[(name, key[1], new_version)] = "draft"

        return new_version

    def get_version(self, name: str, scope: Dict, version: int) -> Optional[Dict]:
        key = self._key(name, scope)
        if key not in self.configs:
            return None
        return self.configs[key].get(version)

    def get_latest_version(self, name: str, scope: Dict) -> Optional[int]:
        key = self._key(name, scope)
        versions = self.configs.get(key)
        if not versions:
            return None
        return max(versions.keys())

    def get_active_version(self, name: str, scope: Dict) -> Optional[int]:
        return self.active_versions.get(self._key(name, scope))

    def set_active_version(self, name: str, scope: Dict, version: int) -> None:
        key = self._key(name, scope)
        self.active_versions[key] = version
        self.version_status[(name, key[1], version)] = "active"

    def get_version_status(self, name: str, scope: Dict, version: int) -> str:
        return self.version_status.get((name, self._key(name, scope)[1], version), "draft")

    def is_version_active(self, name: str, scope: Dict, version: int) -> bool:
        return self.active_versions.get(self._key(name, scope)) == version

    def get_config_entry(self, name: str, scope: Dict, version: int = None) -> Optional[Dict]:
        if version is None:
            version = self.get_active_version(name, scope)
            if version is None:
                version = self.get_latest_version(name, scope)
            if version is None:
                return None
        return self.get_version(name, scope, version)

    def set_binding(self, name: str, scope: Dict, schema_ref: Dict) -> Dict:
        binding = {
            "name": name,
            "scope": deepcopy(scope),
            "schema_ref": deepcopy(schema_ref),
            "active": True
        }
        self.bindings[self._key(name, scope)] = binding
        return binding

    def get_binding(self, name: str, scope: Dict) -> Optional[Dict]:
        return self.bindings.get(self._key(name, scope))

    def get_all_configs(self) -> List[Tuple[str, Dict, int, Dict]]:
        """Get all config entries as (name, scope, version, entry)."""
        results = []
        for (name, scope_key), versions in self.configs.items():
            scope = self.key_to_scope(scope_key)
            for version, entry in versions.items():
                results.append((name, scope, version, entry))
        return results

    def resolve_config(self, name: str, scope: Dict, version: int = None,
                       visited: set = None) -> Tuple[Dict, List[Dict]]:
        if visited is None:
            visited = set()

        scope_key = self.scope_to_key(scope)
        visit_key = (name, scope_key, version)
        if visit_key in visited:
            raise ValueError(f"Circular include detected: {name}")
        visited.add(visit_key)

        entry = self.get_config_entry(name, scope, version)
        if entry is None:
            raise ValueError(f"Config not found: {name}")

        config = deepcopy(entry["config"])
        includes = entry.get("includes", [])

        inheritance_chain = [{
            "name": name,
            "scope": deepcopy(scope),
            "version": version or self.get_latest_version(name, scope)
        }]

        merged = {}
        for inc in includes:
            inc_name = inc["name"]
            inc_scope = inc.get("scope", {})
            inc_version = inc.get("version")

            merged_scope = deepcopy(scope)
            merged_scope.update(inc_scope)

            inc_config, inc_chain = self.resolve_config(
                inc_name, merged_scope, inc_version, visited.copy()
            )
            inheritance_chain.extend(inc_chain)

            merged = deep_merge(merged, inc_config)

        merged = deep_merge(merged, config)

        return merged, inheritance_chain


def deep_merge(base: Dict, override: Dict) -> Dict:
    """Deep merge two dictionaries, with override values taking precedence."""
    result = deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


class ProposalStore:
    """Store for proposals, reviews, and approval policies."""

    def __init__(self):
        self.proposals: Dict[int, Dict] = {}
        self.next_proposal_id: int = 1
        self.policies: Dict[Tuple[str, str], Dict] = {}  # (name, scope_key) -> policy

    def create_proposal(self, name: str, scope: Dict, draft_version: int,
                        base_version: int, author: str, title: str = None,
                        description: str = None, labels: List[str] = None,
                        quorum: Dict = None, diffs: Dict = None,
                        policy_summary: Dict = None) -> int:
        """Create a new proposal and return its ID."""
        proposal_id = self.next_proposal_id
        self.next_proposal_id += 1

        proposal = {
            "proposal_id": proposal_id,
            "name": name,
            "scope": deepcopy(scope),
            "draft_version": draft_version,
            "base_version": base_version,
            "author": author,
            "title": title,
            "description": description,
            "labels": sorted(labels) if labels else [],
            "quorum": deepcopy(quorum) if quorum else {
                "required_approvals": 2,
                "allow_author_approval": False,
                "allowed_reviewers": None
            },
            "status": "open",
            "tally": {
                "approvals": 0,
                "rejections": 0,
                "by_actor": {}
            },
            "diffs": diffs or {
                "raw_json_patch": [],
                "resolved_json_patch": [],
                "includes_changes": [],
                "human": []
            },
            "policy_summary": policy_summary
        }

        self.proposals[proposal_id] = proposal
        return proposal_id

    def get_proposal(self, proposal_id: int) -> Optional[Dict]:
        return self.proposals.get(proposal_id)

    def update_proposal(self, proposal_id: int, updates: Dict) -> None:
        if proposal_id in self.proposals:
            self.proposals[proposal_id].update(updates)

    def _find_proposals(self, name: str, scope: Dict, status: str = None,
                        draft_version: int = None) -> List[Dict]:
        scope_key = ConfigStore.scope_to_key(scope)
        results = []
        for p in self.proposals.values():
            if p["name"] != name or ConfigStore.scope_to_key(p["scope"]) != scope_key:
                continue
            if draft_version is not None and p["draft_version"] != draft_version:
                continue
            if status is not None and p["status"] != status:
                continue
            results.append(p)
        return results

    def list_proposals(self, name: str, scope: Dict, status: str = None) -> List[Dict]:
        results = self._find_proposals(name, scope, status)
        return sorted(results, key=lambda p: p["proposal_id"])

    def get_open_proposals_for_draft(self, name: str, scope: Dict, draft_version: int) -> List[Dict]:
        return self._find_proposals(name, scope, status="open", draft_version=draft_version)

    def get_open_proposals_for_identity(self, name: str, scope: Dict) -> List[Dict]:
        return self._find_proposals(name, scope, status="open")

    def supersede_proposals(self, proposal_ids: List[int]) -> None:
        for pid in proposal_ids:
            if pid in self.proposals:
                self.proposals[pid]["status"] = "superseded"

    def add_review(self, proposal_id: int, actor: str, decision: str,
                   message: str = None) -> None:
        proposal = self.proposals.get(proposal_id)
        if not proposal:
            return

        tally = proposal["tally"]
        by_actor = tally["by_actor"]

        if actor in by_actor:
            prev_decision = by_actor[actor].get("decision")
            if prev_decision == "approve":
                tally["approvals"] -= 1
            elif prev_decision == "reject":
                tally["rejections"] -= 1

        by_actor[actor] = {
            "decision": decision,
            "message": message
        }

        if decision == "approve":
            tally["approvals"] += 1
        elif decision == "reject":
            tally["rejections"] += 1

        proposal["status"] = self._calculate_status(proposal)

    def _calculate_status(self, proposal: Dict) -> str:
        if proposal["status"] in ("merged", "withdrawn", "superseded"):
            return proposal["status"]

        tally = proposal["tally"]
        quorum = proposal["quorum"]

        if tally["rejections"] > 0:
            return "rejected"

        required = quorum.get("required_approvals", 2)
        allow_author = quorum.get("allow_author_approval", False)
        author = proposal.get("author")

        distinct_approvers = set()
        for actor, review in tally["by_actor"].items():
            if review.get("decision") == "approve":
                if allow_author or actor != author:
                    distinct_approvers.add(actor)

        if len(distinct_approvers) >= required:
            return "approved"

        return "open"

    def set_policy(self, name: str, scope: Dict, required_approvals: int,
                   allow_author_approval: bool, allowed_reviewers: List[str] = None) -> Dict:
        """Set approval policy for a config identity."""
        scope_key = ConfigStore.scope_to_key(scope)
        full_key = (name, scope_key)

        policy = {
            "required_approvals": required_approvals,
            "allow_author_approval": allow_author_approval,
            "allowed_reviewers": sorted(allowed_reviewers) if allowed_reviewers else None
        }

        self.policies[full_key] = policy
        return policy

    def get_policy(self, name: str, scope: Dict) -> Dict:
        """Get approval policy for a config identity, with defaults."""
        scope_key = ConfigStore.scope_to_key(scope)
        full_key = (name, scope_key)

        if full_key in self.policies:
            return deepcopy(self.policies[full_key])

        return {
            "required_approvals": 2,
            "allow_author_approval": False,
            "allowed_reviewers": None
        }

    def count_proposals_for_identity(self, name: str, scope: Dict) -> int:
        return len(self._find_proposals(name, scope))

    def count_reviews_for_proposal(self, proposal_id: int) -> int:
        """Count reviews for a proposal."""
        proposal = self.proposals.get(proposal_id)
        if not proposal:
            return 0
        return len(proposal["tally"]["by_actor"])


# ---------------------------------------------------------------------------
# Policy guardrail layer
# ---------------------------------------------------------------------------


class PolicyBundleStore:
    """Manages immutable policy bundles with versioning."""

    MAX_BUNDLES = 500
    MAX_VERSIONS_PER_BUNDLE = 200
    MAX_REGO_SIZE = 1024 * 1024  # 1 MiB

    def __init__(self):
        # bundle_name -> {version: {rego_modules, data, metadata}}
        self.bundles: Dict[str, Dict[int, Dict]] = {}
        self._lock = threading.Lock()

    def create_version(self, bundle_name: str, rego_modules: Dict[str, str],
                       data: Dict = None, metadata: Dict = None) -> int:
        with self._lock:
            if bundle_name not in self.bundles:
                if len(self.bundles) >= self.MAX_BUNDLES:
                    raise ValueError("Maximum number of bundles exceeded")
                self.bundles[bundle_name] = {}

            versions = self.bundles[bundle_name]
            if len(versions) >= self.MAX_VERSIONS_PER_BUNDLE:
                raise ValueError("Maximum versions per bundle exceeded")

            # Check combined size
            total_size = sum(len(v.encode('utf-8')) for v in rego_modules.values())
            if total_size > self.MAX_REGO_SIZE:
                raise ValueError("rego_modules exceed 1 MiB limit")

            new_version = max(versions.keys(), default=0) + 1
            versions[new_version] = {
                "rego_modules": deepcopy(rego_modules),
                "data": normalize_value(deepcopy(data)) if data else {},
                "metadata": normalize_value(deepcopy(metadata)) if metadata else {}
            }
            return new_version

    def get_version(self, bundle_name: str, version: int) -> Optional[Dict]:
        if bundle_name not in self.bundles:
            return None
        return self.bundles[bundle_name].get(version)

    def list_versions(self, bundle_name: str) -> List[int]:
        if bundle_name not in self.bundles:
            return []
        return sorted(self.bundles[bundle_name].keys())

    def exists(self, bundle_name: str, version: int) -> bool:
        return bundle_name in self.bundles and version in self.bundles[bundle_name]

    def get_versions_with_metadata(self, bundle_name: str) -> List[Dict]:
        if bundle_name not in self.bundles:
            return []
        result = []
        for v in sorted(self.bundles[bundle_name].keys()):
            entry = self.bundles[bundle_name][v]
            result.append({
                "version": v,
                "metadata": entry.get("metadata", {})
            })
        return result


class PolicyBindingStore:
    """Manages policy bindings associating bundles with selectors."""

    MAX_BINDINGS = 5000

    def __init__(self):
        self.bindings: Dict[str, Dict] = {}  # binding_id -> binding
        self._next_id = 1
        self._lock = threading.Lock()

    def create_binding(self, bundle_name: str, bundle_version: int,
                       selector: Dict, graph_keys: List[str] = None,
                       priority: int = 0) -> Tuple[str, Dict]:
        with self._lock:
            if len(self.bindings) >= self.MAX_BINDINGS:
                raise ValueError("Maximum bindings exceeded")

            binding_id = str(self._next_id)
            self._next_id += 1

            binding = {
                "binding_id": binding_id,
                "bundle": {
                    "name": bundle_name,
                    "version": bundle_version
                },
                "selector": normalize_value(deepcopy(selector)),
                "graph_keys": sorted(graph_keys) if graph_keys else ["env", "tenant"],
                "priority": priority
            }

            self.bindings[binding_id] = binding
            return binding_id, deepcopy(binding)

    def check_duplicate(self, bundle_name: str, bundle_version: int,
                        selector: Dict, priority: int) -> bool:
        """Check if a binding with same bundle+selector+priority exists."""
        sel_normalized = normalize_value(selector)
        for b in self.bindings.values():
            if (b["bundle"]["name"] == bundle_name and
                b["bundle"]["version"] == bundle_version and
                b["selector"] == sel_normalized and
                b["priority"] == priority):
                return True
        return False

    def get_matching_bindings(self, name: str, scope: Dict) -> List[Dict]:
        """Get all bindings whose selector matches the given scope, sorted by priority."""
        matches = []
        for b in self.bindings.values():
            selector = b["selector"]
            if self._selector_matches(selector, scope):
                matches.append(b)

        # Sort by priority descending, then bundle name ascending, then bundle version ascending
        matches.sort(key=lambda b: (-b["priority"], b["bundle"]["name"], b["bundle"]["version"]))
        return matches

    @staticmethod
    def _selector_matches(selector: Dict, scope: Dict) -> bool:
        """Exact-match selector against scope."""
        for k, v in selector.items():
            if k not in scope or scope[k] != v:
                return False
        return True

    def get_binding(self, binding_id: str) -> Optional[Dict]:
        return self.bindings.get(binding_id)


# ---------------------------------------------------------------------------
# Rego-like policy engine
# ---------------------------------------------------------------------------


class RegoEngine:
    """Minimal deterministic Rego-like policy evaluator."""

    @staticmethod
    def evaluate(rego_modules: Dict[str, str], input_data: Dict,
                 bundle_data: Dict = None, timeout_ms: int = 500) -> Dict:
        """
        Evaluate rego modules against input, returning data.guardrails.
        Returns {"deny": [...], "warn": [...]}
        """
        result = {"deny": [], "warn": []}

        # Build the evaluation context
        ctx = {
            "input": input_data,
            "data": bundle_data or {}
        }

        # Parse and evaluate all modules
        all_rules = []
        for module_name, module_code in rego_modules.items():
            try:
                rules = RegoParser.parse(module_code)
                all_rules.extend(rules)
            except Exception:
                continue

        # Evaluate deny rules
        for rule in all_rules:
            if rule.get("kind") == "deny":
                violations = RegoEvaluator.eval_set_rule(rule, ctx)
                for v in violations:
                    if isinstance(v, dict):
                        result["deny"].append(v)
            elif rule.get("kind") == "warn":
                violations = RegoEvaluator.eval_set_rule(rule, ctx)
                for v in violations:
                    if isinstance(v, dict):
                        result["warn"].append(v)
            elif rule.get("kind") == "complete":
                # Complete rules define data values
                name = rule.get("name", "")
                if name == "prod_target" or name.startswith("_"):
                    continue
                val = RegoEvaluator.eval_complete_rule(rule, ctx)
                # Store computed value in context
                parts = name.split(".")
                target = ctx
                for p in parts[:-1]:
                    if p not in target:
                        target[p] = {}
                    target = target[p]
                if parts:
                    target[parts[-1]] = val

        return result


class RegoParser:
    """Parse simplified Rego into rule ASTs."""

    @staticmethod
    def parse(code: str) -> List[Dict]:
        """Parse rego code into a list of rule dicts."""
        rules = []
        lines = code.split('\n')

        # Track package and helper rules
        current_package = None
        i = 0

        while i < len(lines):
            line = lines[i].strip()
            i += 1

            if not line or line.startswith('#'):
                continue

            # Package declaration
            if line.startswith('package '):
                current_package = line[8:].strip()
                continue

            # Import (skip)
            if line.startswith('import '):
                continue

            # Parse deny contains {...} if { ... } or warn contains {...} if { ... }
            # This is the newer Rego syntax
            m = re.match(r'^(deny|warn)\s+contains\s+(\{[^}]*\})\s+if\s*\{', line)
            if m:
                kind = m.group(1)
                violation_obj_str = m.group(2)
                # Parse the violation object
                try:
                    # Convert Rego object to JSON-like format for parsing
                    violation_obj = RegoParser._parse_rego_object(violation_obj_str)
                except:
                    violation_obj = {"raw": violation_obj_str}

                body_lines = []
                depth = 1
                while i < len(lines) and depth > 0:
                    l = lines[i].strip()
                    depth += l.count('{') - l.count('}')
                    if depth > 0:
                        body_lines.append(l)
                    else:
                        remaining = l
                        if remaining.endswith('}'):
                            remaining = remaining[:-1].strip()
                        if remaining:
                            body_lines.append(remaining)
                    i += 1
                rules.append({
                    "kind": kind,
                    "var_name": "violation",
                    "violation_template": violation_obj,
                    "body": body_lines
                })
                continue

            # Parse deny[violation] { ... } or warn[violation] { ... } (older syntax)
            m = re.match(r'^(deny|warn)\s*\[(\w+)\]\s*\{', line)
            if m:
                kind = m.group(1)
                var_name = m.group(2)
                body_lines = []
                depth = 1
                while i < len(lines) and depth > 0:
                    l = lines[i].strip()
                    depth += l.count('{') - l.count('}')
                    if depth > 0:
                        body_lines.append(l)
                    else:
                        # Last line might have content before the closing }
                        remaining = l
                        if remaining.endswith('}'):
                            remaining = remaining[:-1].strip()
                        if remaining:
                            body_lines.append(remaining)
                    i += 1
                rules.append({
                    "kind": kind,
                    "var_name": var_name,
                    "body": body_lines
                })
                continue

            # Parse complete rule with 'if': name if { body }
            m = re.match(r'^(\w+)\s+if\s*\{', line)
            if m:
                rule_name = m.group(1)
                body_lines = []
                depth = 1
                while i < len(lines) and depth > 0:
                    l = lines[i].strip()
                    depth += l.count('{') - l.count('}')
                    if depth > 0:
                        body_lines.append(l)
                    else:
                        remaining = l
                        if remaining.endswith('}'):
                            remaining = remaining[:-1].strip()
                        if remaining:
                            body_lines.append(remaining)
                    i += 1
                rules.append({
                    "kind": "complete",
                    "name": rule_name,
                    "default_value": None,
                    "body": body_lines
                })
                continue

            # Parse complete rule: name = value { body } or name { body }
            m = re.match(r'^(\w+)\s*(?:=\s*([^{\n]+))?\s*\{', line)
            if m:
                rule_name = m.group(1)
                default_val = m.group(2)
                if default_val:
                    default_val = default_val.strip()
                body_lines = []
                depth = 1
                while i < len(lines) and depth > 0:
                    l = lines[i].strip()
                    depth += l.count('{') - l.count('}')
                    if depth > 0:
                        body_lines.append(l)
                    else:
                        remaining = l
                        if remaining.endswith('}'):
                            remaining = remaining[:-1].strip()
                        if remaining:
                            body_lines.append(remaining)
                    i += 1
                rules.append({
                    "kind": "complete",
                    "name": rule_name,
                    "default_value": default_val,
                    "body": body_lines
                })
                continue

            # Parse assignment: var := expr
            m = re.match(r'^(\w+)\s*:?=\s*(.+)$', line)
            if m:
                var_name = m.group(1)
                expr = m.group(2).strip()
                rules.append({
                    "kind": "assignment",
                    "name": var_name,
                    "expr": expr
                })
                continue

        return rules

    @staticmethod
    def _parse_rego_object(obj_str: str) -> Dict:
        """Parse a Rego object literal like {"id": "X", "message": "Y"}."""
        # Simple parser for object literals
        result = {}
        # Remove outer braces
        obj_str = obj_str.strip()
        if obj_str.startswith('{') and obj_str.endswith('}'):
            obj_str = obj_str[1:-1]

        # Split by comma, handling nested structures
        parts = []
        current = ""
        depth = 0
        for char in obj_str:
            if char == '{' or char == '[':
                depth += 1
            elif char == '}' or char == ']':
                depth -= 1
            elif char == ',' and depth == 0:
                parts.append(current.strip())
                current = ""
                continue
            current += char
        if current.strip():
            parts.append(current.strip())

        for part in parts:
            if ':' in part:
                key, val = part.split(':', 1)
                key = key.strip().strip('"')
                val = val.strip()
                # Parse the value
                if val.startswith('"') and val.endswith('"'):
                    result[key] = val[1:-1]
                elif val.startswith("'") and val.endswith("'"):
                    result[key] = val[1:-1]
                elif val == 'true':
                    result[key] = True
                elif val == 'false':
                    result[key] = False
                elif val == 'null':
                    result[key] = None
                else:
                    try:
                        result[key] = int(val)
                    except ValueError:
                        try:
                            result[key] = float(val)
                        except ValueError:
                            result[key] = val

        return result


class RegoEvaluator:
    """Evaluate parsed Rego rules against input context."""

    @staticmethod
    def eval_set_rule(rule: Dict, ctx: Dict) -> List:
        """Evaluate a set rule (deny/warn) and return violations."""
        body = rule.get("body", [])
        var_name = rule.get("var_name", "violation")
        violation_template = rule.get("violation_template")

        # Check if all body conditions are satisfied
        local_vars = {}
        assignment_expr = None

        for line in body:
            line = line.strip()
            if not line:
                continue

            # Assignment: var := expr
            m = re.match(r'^(\w+)\s*:?=\s*(.+)$', line)
            if m:
                lname = m.group(1)
                lexpr = m.group(2).strip()
                if lname == var_name:
                    assignment_expr = lexpr
                else:
                    local_vars[lname] = RegoEvaluator._eval_expr(lexpr, ctx, local_vars)
                continue

            # Condition check
            if not RegoEvaluator._eval_condition(line, ctx, local_vars):
                return []

        # If there's a violation template from "deny contains {...} if {...}" syntax
        if violation_template:
            return [violation_template.copy()]

        # If there's an assignment for the violation variable, evaluate it
        if assignment_expr:
            val = RegoEvaluator._eval_expr(assignment_expr, ctx, local_vars)
            if isinstance(val, list):
                return val
            return [val]

        return [{}]

    @staticmethod
    def eval_complete_rule(rule: Dict, ctx: Dict) -> Any:
        """Evaluate a complete rule."""
        body = rule.get("body", [])

        if not body:
            # No body means unconditional
            default = rule.get("default_value")
            if default is not None:
                return RegoEvaluator._eval_expr(default, ctx, {})
            return True

        local_vars = {}
        for line in body:
            line = line.strip()
            if not line:
                continue

            m = re.match(r'^(\w+)\s*:?=\s*(.+)$', line)
            if m:
                lname = m.group(1)
                lexpr = m.group(2).strip()
                local_vars[lname] = RegoEvaluator._eval_expr(lexpr, ctx, local_vars)
                continue

            if not RegoEvaluator._eval_condition(line, ctx, local_vars):
                return None

        default = rule.get("default_value")
        if default is not None:
            return RegoEvaluator._eval_expr(default, ctx, local_vars)
        return True

    @staticmethod
    def _eval_condition(expr: str, ctx: Dict, local_vars: Dict) -> bool:
        """Evaluate a boolean condition."""
        expr = expr.strip()

        # Handle 'not' prefix
        if expr.startswith('not '):
            inner = expr[4:].strip()
            return not RegoEvaluator._eval_truthy(inner, ctx, local_vars)

        return RegoEvaluator._eval_truthy(expr, ctx, local_vars)

    @staticmethod
    def _eval_truthy(expr: str, ctx: Dict, local_vars: Dict) -> bool:
        """Evaluate expression as truthy."""
        expr = expr.strip()

        # Binary comparisons
        for op in ['!=', '==', '>=', '<=', '>', '<']:
            # Find operator not inside strings or parens
            idx = RegoEvaluator._find_operator(expr, op)
            if idx >= 0:
                left = expr[:idx].strip()
                right = expr[idx + len(op):].strip()
                left_val = RegoEvaluator._eval_expr(left, ctx, local_vars)
                right_val = RegoEvaluator._eval_expr(right, ctx, local_vars)
                return RegoEvaluator._compare(left_val, right_val, op)

        # Simple truthy check
        val = RegoEvaluator._eval_expr(expr, ctx, local_vars)
        return bool(val)

    @staticmethod
    def _find_operator(expr: str, op: str) -> int:
        """Find operator position in expression, ignoring strings and parens."""
        in_string = False
        string_char = None
        paren_depth = 0
        i = 0
        while i < len(expr) - len(op) + 1:
            c = expr[i]
            if in_string:
                if c == string_char and (i == 0 or expr[i-1] != '\\'):
                    in_string = False
                i += 1
                continue
            if c in ('"', "'"):
                in_string = True
                string_char = c
                i += 1
                continue
            if c == '(':
                paren_depth += 1
                i += 1
                continue
            if c == ')':
                paren_depth -= 1
                i += 1
                continue
            if paren_depth == 0 and expr[i:i+len(op)] == op:
                # Make sure we're not matching a longer operator
                if op == '!=' and i + 2 < len(expr) and expr[i+2] == '=':
                    i += 1
                    continue
                if op == '==' and i > 0 and expr[i-1] == '!':
                    i += 1
                    continue
                return i
            i += 1
        return -1

    @staticmethod
    def _compare(left, right, op: str) -> bool:
        """Compare two values."""
        if op == '==':
            return left == right
        elif op == '!=':
            return left != right
        elif op == '>':
            return left > right
        elif op == '<':
            return left < right
        elif op == '>=':
            return left >= right
        elif op == '<=':
            return left <= right
        return False

    @staticmethod
    def _eval_expr(expr: str, ctx: Dict, local_vars: Dict) -> Any:
        """Evaluate an expression."""
        expr = expr.strip()

        if not expr:
            return None

        # String literal
        if (expr.startswith('"') and expr.endswith('"')) or \
           (expr.startswith("'") and expr.endswith("'")):
            return expr[1:-1]

        # Boolean literal
        if expr == 'true':
            return True
        if expr == 'false':
            return False

        # Null literal
        if expr == 'null':
            return None

        # Number literal
        try:
            if '.' in expr:
                return float(expr)
            return int(expr)
        except (ValueError, TypeError):
            pass

        # Object literal { key: value, ... }
        if expr.startswith('{') and expr.endswith('}'):
            inner = expr[1:-1].strip()
            if not inner:
                return {}
            # Check if it's a set (just values) or object (key: value pairs)
            return RegoEvaluator._parse_object_literal(inner, ctx, local_vars)

        # Array literal [...]
        if expr.startswith('[') and expr.endswith(']'):
            inner = expr[1:-1].strip()
            if not inner:
                return []
            items = RegoEvaluator._split_by_comma(inner)
            return [RegoEvaluator._eval_expr(item.strip(), ctx, local_vars) for item in items]

        # Function call: count(...), etc.
        m = re.match(r'^(\w+)\s*\((.+)\)$', expr)
        if m:
            func_name = m.group(1)
            func_args_str = m.group(2)
            args = RegoEvaluator._split_by_comma(func_args_str)
            evaluated_args = [RegoEvaluator._eval_expr(a.strip(), ctx, local_vars) for a in args]
            return RegoEvaluator._call_function(func_name, evaluated_args)

        # Parenthesized expression
        if expr.startswith('(') and expr.endswith(')'):
            return RegoEvaluator._eval_expr(expr[1:-1], ctx, local_vars)

        # Reference: input.target.scope.env, etc.
        # Could also be a local variable
        return RegoEvaluator._resolve_ref(expr, ctx, local_vars)

    @staticmethod
    def _parse_object_literal(inner: str, ctx: Dict, local_vars: Dict) -> Dict:
        """Parse a Rego object literal."""
        result = {}
        # Simple key-value parsing
        parts = RegoEvaluator._split_by_comma(inner)
        for part in parts:
            part = part.strip()
            # key: value or "key": value
            colon_idx = part.find(':')
            if colon_idx > 0:
                key_expr = part[:colon_idx].strip()
                val_expr = part[colon_idx+1:].strip()
                key = RegoEvaluator._eval_expr(key_expr, ctx, local_vars)
                val = RegoEvaluator._eval_expr(val_expr, ctx, local_vars)
                if isinstance(key, str):
                    result[key] = val
        return result

    @staticmethod
    def _split_by_comma(s: str) -> List[str]:
        """Split string by commas, respecting nesting."""
        parts = []
        depth = 0
        current = []
        in_string = False
        string_char = None
        i = 0
        while i < len(s):
            c = s[i]
            if in_string:
                current.append(c)
                if c == string_char and (i == 0 or s[i-1] != '\\'):
                    in_string = False
                i += 1
                continue
            if c in ('"', "'"):
                in_string = True
                string_char = c
                current.append(c)
                i += 1
                continue
            if c in ('(', '[', '{'):
                depth += 1
                current.append(c)
            elif c in (')', ']', '}'):
                depth -= 1
                current.append(c)
            elif c == ',' and depth == 0:
                parts.append(''.join(current))
                current = []
            else:
                current.append(c)
            i += 1
        if current:
            parts.append(''.join(current))
        return parts

    @staticmethod
    def _call_function(name: str, args: List) -> Any:
        """Call a built-in function."""
        if name == 'count' and len(args) == 1:
            val = args[0]
            if isinstance(val, (list, dict, str)):
                return len(val)
            return 0
        if name == 'len' and len(args) == 1:
            val = args[0]
            if isinstance(val, (list, dict, str)):
                return len(val)
            return 0
        if name == 'max' and len(args) == 2:
            return max(args[0], args[1])
        if name == 'min' and len(args) == 2:
            return min(args[0], args[1])
        if name == 'concat' and len(args) == 2:
            sep = args[0]
            items = args[1] if isinstance(args[1], list) else [args[1]]
            return sep.join(str(x) for x in items)
        if name == 'contains' and len(args) == 2:
            return args[1] in args[0] if isinstance(args[0], str) else False
        if name == 'lower' and len(args) == 1:
            return str(args[0]).lower()
        if name == 'upper' and len(args) == 1:
            return str(args[0]).upper()
        if name == 'trim' and len(args) == 1:
            return str(args[0]).strip()
        if name == 'sprintf' and len(args) >= 1:
            fmt = args[0]
            fmt_args = args[1] if len(args) > 1 else []
            if isinstance(fmt_args, list):
                try:
                    return fmt % tuple(fmt_args)
                except (TypeError, ValueError):
                    return fmt
            return fmt
        if name == 'json_marshal' and len(args) == 1:
            return json.dumps(args[0])
        if name == 'object' and len(args) == 1:
            return args[0] if isinstance(args[0], dict) else {}
        if name == 'array_slice' and len(args) == 3:
            arr, start, end = args
            if isinstance(arr, list):
                return arr[int(start):int(end)]
            return []
        if name == 'sort' and len(args) == 1:
            if isinstance(args[0], list):
                return sorted(args[0])
            return args[0]
        if name == 'unique' and len(args) == 1:
            if isinstance(args[0], list):
                return sorted(set(str(x) for x in args[0]))
            return args[0]
        if name == 'abs' and len(args) == 1:
            return abs(args[0])
        if name == 'ceil' and len(args) == 1:
            import math
            return math.ceil(args[0])
        if name == 'floor' and len(args) == 1:
            import math
            return math.floor(args[0])
        return None

    @staticmethod
    def _resolve_ref(ref: str, ctx: Dict, local_vars: Dict) -> Any:
        """Resolve a dotted reference like input.target.scope.env."""
        # Check local vars first
        parts = ref.split('.')
        if parts[0] in local_vars:
            val = local_vars[parts[0]]
            for part in parts[1:]:
                if isinstance(val, dict):
                    val = val.get(part)
                elif isinstance(val, list):
                    try:
                        val = val[int(part)]
                    except (ValueError, IndexError):
                        return None
                else:
                    return None
            return val

        # Check input/data context
        if parts[0] in ctx:
            val = ctx[parts[0]]
            for part in parts[1:]:
                if isinstance(val, dict):
                    val = val.get(part)
                elif isinstance(val, list):
                    try:
                        val = val[int(part)]
                    except (ValueError, IndexError):
                        return None
                else:
                    return None
            return val

        # Check if it's a bare reference to a helper rule name
        # (e.g., prod_target references a rule defined elsewhere)
        if len(parts) == 1:
            return local_vars.get(parts[0])

        return None


def _build_policy_input(target_name: str, target_scope: Dict,
                        target_version: int, target_resolved: Dict,
                        target_inheritance: List[Dict],
                        graph_by_name: Dict[str, Dict],
                        now: str) -> Dict:
    """Build the input object for policy evaluation."""
    return {
        "target": {
            "name": target_name,
            "scope": normalize_value(target_scope),
            "version_used": target_version,
            "resolved_config": normalize_value(target_resolved),
            "provenance": target_inheritance
        },
        "graph": {
            "by_name": graph_by_name
        },
        "now": now
    }


def _now_rfc3339() -> str:
    """Return current UTC time as RFC3339 with Z suffix."""
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _gather_graph(config_store: 'ConfigStore', target_name: str,
                  target_scope: Dict, graph_keys: List[str],
                  target_version: int, target_resolved: Dict,
                  target_inheritance: List[Dict]) -> Dict[str, Dict]:
    """Gather the graph of related configs matching on graph_keys."""
    # Build the target's graph scope
    target_scope_normalized = normalize_value(target_scope)
    graph_filter = {}
    for k in graph_keys:
        if k in target_scope_normalized:
            graph_filter[k] = target_scope_normalized[k]

    if not graph_filter:
        # Always include at least the target itself
        return {target_name: normalize_value(target_resolved)}

    # Find all configs whose scope matches on all graph_keys
    candidates = []
    for (name, scope_key), versions in config_store.configs.items():
        scope = config_store.key_to_scope(scope_key)
        scope_normalized = normalize_value(scope)

        # Check if all graph_keys match
        matches = True
        for k, v in graph_filter.items():
            if scope_normalized.get(k) != v:
                matches = False
                break

        if matches:
            # Get active version or latest
            active_v = config_store.get_active_version(name, scope)
            if active_v is None:
                active_v = max(versions.keys()) if versions else None
            if active_v is not None:
                candidates.append((name, scope, active_v))

    # Also include the target itself
    target_entry = (target_name, target_scope, target_version)
    found = False
    for c in candidates:
        if c[0] == target_name and config_store.scope_to_key(c[1]) == config_store.scope_to_key(target_scope):
            found = True
            break
    if not found:
        candidates.append(target_entry)

    # Sort by name ascending and truncate at 2000
    candidates.sort(key=lambda x: x[0])
    truncated = len(candidates) > 2000
    if truncated:
        candidates = candidates[:2000]

    # Build by_name map
    by_name = {}
    for name, scope, version in candidates:
        try:
            resolved, _ = config_store.resolve_config(name, scope, version)
            by_name[name] = normalize_value(resolved)
        except (ValueError, KeyError):
            # If we can't resolve, use the stored config
            entry = config_store.get_version(name, scope, version)
            if entry:
                by_name[name] = normalize_value(entry["config"])

    # Sort by_name by key
    by_name = dict(sorted(by_name.items()))

    return by_name, truncated


def evaluate_policies_for_target(config_store: 'ConfigStore',
                                 bundle_store: 'PolicyBundleStore',
                                 binding_store: 'PolicyBindingStore',
                                 target_name: str, target_scope: Dict,
                                 target_version: int = None,
                                 include_graph: bool = True) -> Tuple[Dict, bool]:
    """
    Evaluate all applicable policies for a target config.
    Returns (evaluation_result, timed_out).
    """
    # Resolve target config
    try:
        target_resolved, target_inheritance = config_store.resolve_config(
            target_name, target_scope, target_version
        )
    except (ValueError, KeyError) as e:
        # Can't resolve target
        return {
            "policy_stack": [],
            "violations": [],
            "tally": {"errors": 0, "warnings": 0}
        }, False

    # Use provided version or active/latest
    if target_version is None:
        target_version = config_store.get_active_version(target_name, target_scope)
        if target_version is None:
            target_version = config_store.get_latest_version(target_name, target_scope)

    # Get matching bindings
    matching_bindings = binding_store.get_matching_bindings(target_name, target_scope)

    if not matching_bindings:
        return {
            "policy_stack": [],
            "violations": [],
            "tally": {"errors": 0, "warnings": 0}
        }, False

    # Build policy stack description
    policy_stack = []
    for b in matching_bindings:
        policy_stack.append({
            "bundle": {"name": b["bundle"]["name"], "version": b["bundle"]["version"]},
            "selector": b["selector"],
            "graph_keys": b["graph_keys"],
            "priority": b["priority"]
        })

    # Gather graph using the union of all graph_keys from matching bindings
    all_graph_keys = set()
    for b in matching_bindings:
        all_graph_keys.update(b["graph_keys"])
    graph_keys = sorted(all_graph_keys)

    if include_graph:
        by_name, graph_truncated = _gather_graph(
            config_store, target_name, target_scope, graph_keys,
            target_version, target_resolved, target_inheritance
        )
    else:
        by_name = {target_name: normalize_value(target_resolved)}
        graph_truncated = False

    now = _now_rfc3339()

    all_violations = []
    truncated = False
    timed_out = False
    start_time = time.monotonic()

    for b in matching_bindings:
        # Check timeout
        elapsed_ms = (time.monotonic() - start_time) * 1000
        if elapsed_ms > 500:
            timed_out = True
            break

        bundle_name = b["bundle"]["name"]
        bundle_version = b["bundle"]["version"]

        bundle = bundle_store.get_version(bundle_name, bundle_version)
        if bundle is None:
            continue

        # Build input
        input_data = _build_policy_input(
            target_name, target_scope, target_version,
            normalize_value(target_resolved), target_inheritance,
            by_name, now
        )

        try:
            rego_result = RegoEngine.evaluate(
                bundle["rego_modules"],
                input_data,
                bundle.get("data", {}),
                timeout_ms=max(1, int(500 - elapsed_ms))
            )
        except Exception:
            rego_result = {"deny": [], "warn": []}

        # Process deny violations
        for v in rego_result.get("deny", []):
            if len(all_violations) >= 1000:
                truncated = True
                break
            violation = {
                "policy": {"name": bundle_name, "version": bundle_version},
                "target": {
                    "name": target_name,
                    "scope": normalize_value(target_scope),
                    "version_used": target_version
                },
                "rule_id": v.get("id", ""),
                "severity": "error",
                "path": v.get("path", ""),
                "message": v.get("message", ""),
            }
            # Include any additional fields as evidence
            evidence = {}
            for k, val in v.items():
                if k not in ("id", "message", "path", "target"):
                    evidence[k] = val
            if evidence:
                violation["evidence"] = evidence
            all_violations.append(violation)

        if truncated:
            break

        # Process warn violations
        for v in rego_result.get("warn", []):
            if len(all_violations) >= 1000:
                truncated = True
                break
            violation = {
                "policy": {"name": bundle_name, "version": bundle_version},
                "target": {
                    "name": target_name,
                    "scope": normalize_value(target_scope),
                    "version_used": target_version
                },
                "rule_id": v.get("id", ""),
                "severity": "warn",
                "path": v.get("path", ""),
                "message": v.get("message", ""),
            }
            evidence = {}
            for k, val in v.items():
                if k not in ("id", "message", "path", "target"):
                    evidence[k] = val
            if evidence:
                violation["evidence"] = evidence
            all_violations.append(violation)

        if truncated:
            break

    # Sort violations lexicographically by (target.name, policy.name, policy.version, rule_id, path)
    all_violations.sort(key=lambda v: (
        v["target"]["name"],
        v["policy"]["name"],
        v["policy"]["version"],
        v["rule_id"],
        v["path"]
    ))

    # Build result
    errors = sum(1 for v in all_violations if v["severity"] == "error")
    warnings = sum(1 for v in all_violations if v["severity"] == "warn")

    result = {
        "policy_stack": policy_stack,
        "violations": all_violations,
        "tally": {"errors": errors, "warnings": warnings}
    }

    if truncated:
        result["truncated"] = True

    if graph_truncated:
        result["details"] = result.get("details", {})
        result["details"]["graph_truncated"] = True

    return result, timed_out


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------


class ConfigServerHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the config server."""

    schemas: SchemaRegistry = None
    configs: ConfigStore = None
    proposals: ProposalStore = None
    policy_bundles: PolicyBundleStore = None
    policy_bindings: PolicyBindingStore = None

    def log_message(self, format, *args):
        pass

    def send_json_response(self, status: int, data: Any):
        body = json.dumps(data, separators=(',', ':')).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def send_error_response(self, status: int, code: str, message: str, details: Dict = None):
        error = {"code": code, "message": message}
        if details:
            error["details"] = details
        self.send_json_response(status, {"error": error})

    def send_validation_error(self, e: SchemaValidationError):
        self.send_error_response(422, "validation_failed",
                                "Config does not conform to schema", {
                                    "path": e.path,
                                    "rule": e.rule,
                                    "expected": e.expected,
                                    "actual": e.actual
                                })

    def resolve_effective_schema(self, name: str, scope: Dict, schema_ref: Dict = None):
        """Return (effective_schema_ref, effective_schema) or (None, None) if sent error."""
        if schema_ref:
            schema_name = schema_ref.get("name")
            schema_version = schema_ref.get("version")
            if not self.schemas.exists(schema_name, schema_version):
                self.send_error_response(404, "not_found", "Schema version not found")
                return None, None
            return schema_ref, self.schemas.get(schema_name, schema_version)
        binding = self.configs.get_binding(name, scope)
        if binding:
            ref = binding["schema_ref"]
            return ref, self.schemas.get(ref.get("name"), ref.get("version"))
        return None, None

    def read_request_body(self) -> bytes:
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length == 0:
            return b'{}'

        if content_length > 1024 * 1024:
            return None

        return self.rfile.read(content_length)

    def parse_json_body(self) -> Tuple[Optional[Dict], Optional[str]]:
        """Parse the request body as JSON."""
        body = self.read_request_body()
        if body is None:
            return None, "too_large"

        try:
            return json.loads(body.decode('utf-8')), None
        except json.JSONDecodeError:
            return None, "invalid_json"

    def parse_path(self) -> Tuple[str, List[str], Dict]:
        """Parse the request path into components."""
        parsed = urlparse(self.path)
        path_parts = [p for p in parsed.path.split('/') if p]
        query_params = parse_qs(parsed.query)
        return parsed.path, path_parts, query_params

    def do_GET(self):
        """Handle GET requests."""
        path, parts, query = self.parse_path()

        if path == '/healthz':
            self.send_json_response(200, {"ok": True})
            return

        self.send_error_response(404, "not_found", "Endpoint not found")

    def do_POST(self):
        path, parts, query = self.parse_path()

        # Allow healthz to work without content-type
        if path == '/healthz':
            self.send_json_response(200, {"ok": True})
            return

        content_type = self.headers.get('Content-Type', '')
        if not content_type.startswith('application/json'):
            self.send_error_response(415, "unsupported_media_type",
                                    "Content-Type must be application/json")
            return

        body, error = self.parse_json_body()
        if error == "too_large":
            self.send_error_response(413, "too_large", "Request body too large")
            return
        if error == "invalid_json":
            self.send_error_response(400, "bad_request", "Invalid JSON body")
            return

        if body is None:
            body = {}

        try:
            self.route_post(path, parts, body)
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            self.send_error_response(500, "internal_error", str(e))

    def route_post(self, path: str, parts: List[str], body: Dict):
        if len(parts) < 2 or parts[0] != 'v1':
            self.send_error_response(404, "not_found", "Endpoint not found")
            return

        if parts[1] == 'schemas':
            self._route_schema(parts, body)
        elif parts[1] == 'configs':
            self._route_config(parts, body)
        elif parts[1] == 'proposals':
            self._route_proposal(parts, body)
        elif parts[1] == 'policies':
            self._route_policy(parts, body)
        else:
            self.send_error_response(404, "not_found", "Endpoint not found")

    def _route_schema(self, parts: List[str], body: Dict):
        if len(parts) < 3:
            self.send_error_response(404, "not_found", "Endpoint not found")
            return
        schema_name = parts[2]

        if len(parts) == 3:
            self.handle_create_schema(schema_name, body)
        elif len(parts) == 4 and parts[3] == 'versions':
            self.handle_list_schema_versions(schema_name, body)
        elif len(parts) == 4:
            try:
                version = int(parts[3])
                self.handle_get_schema_version(schema_name, version, body)
            except ValueError:
                self.send_error_response(400, "bad_request", "Invalid version number")
        else:
            self.send_error_response(404, "not_found", "Endpoint not found")

    def _route_config(self, parts: List[str], body: Dict):
        if len(parts) < 3:
            self.send_error_response(404, "not_found", "Endpoint not found")
            return
        name = parts[2]

        if len(parts) == 3:
            if name.endswith(':policy'):
                self.handle_policy(name[:-7], body)
                return
            if name.endswith(':bind'):
                self.handle_bind_schema(name[:-5], body)
                return
            if name.endswith(':resolve'):
                self.handle_resolve_config(name[:-8], body)
                return
            if name.endswith(':validate'):
                self.handle_validate_config(name[:-9], body)
                return
            if name.endswith(':rollback'):
                self.handle_rollback(name[:-9], body)
                return
            self.handle_create_config(name, body)
            return

        if len(parts) >= 4:
            if parts[3].endswith(':propose'):
                try:
                    version = int(parts[3][:-8])
                    self.handle_propose(name, version, body)
                except ValueError:
                    self.send_error_response(400, "bad_request", "Invalid version number")
                return

            if parts[3].endswith(':activate'):
                try:
                    version = int(parts[3][:-9])
                    self.handle_activate(name, version, body)
                except ValueError:
                    self.send_error_response(400, "bad_request", "Invalid version number")
                return

            if parts[3] == 'versions':
                self.handle_list_config_versions(name, body)
                return

            if len(parts) == 4 and parts[3] == 'proposals:list':
                self.handle_list_proposals(name, body)
                return

            try:
                version = int(parts[3])
                if len(parts) == 4:
                    self.handle_get_config_version(name, version, body)
                elif len(parts) == 5 and parts[4] == 'schema':
                    self.handle_get_binding(name, body)
                else:
                    self.send_error_response(404, "not_found", "Endpoint not found")
            except ValueError:
                self.send_error_response(400, "bad_request", "Invalid version number")

    def _route_proposal(self, parts: List[str], body: Dict):
        if len(parts) < 3:
            self.send_error_response(404, "not_found", "Endpoint not found")
            return

        try:
            proposal_id = int(parts[2])
        except ValueError:
            self.send_error_response(400, "bad_request", "Invalid proposal ID")
            return

        action = parts[3] if len(parts) > 3 else None

        if action == 'get' or (len(parts) == 4 and parts[3].endswith(':get')):
            self.handle_get_proposal(proposal_id, body)
        elif action == 'review' or (len(parts) == 4 and parts[3].endswith(':review')):
            self.handle_review(proposal_id, body)
        elif action == 'merge' or (len(parts) == 4 and parts[3].endswith(':merge')):
            self.handle_merge(proposal_id, body)
        elif action == 'withdraw' or (len(parts) == 4 and parts[3].endswith(':withdraw')):
            self.handle_withdraw(proposal_id, body)
        else:
            self.send_error_response(404, "not_found", "Endpoint not found")

    def _route_policy(self, parts: List[str], body: Dict):
        """Route policy-related endpoints."""
        if len(parts) < 3:
            self.send_error_response(404, "not_found", "Endpoint not found")
            return

        if parts[2] == 'bundles':
            self._route_policy_bundles(parts, body)
        elif parts[2] == 'bindings':
            self._route_policy_bindings(parts, body)
        elif parts[2] == 'stack':
            self.handle_policy_stack(body)
        elif parts[2] == 'evaluate':
            self.handle_policy_evaluate(body)
        elif parts[2] == 'explain':
            self.handle_policy_explain(body)
        else:
            self.send_error_response(404, "not_found", "Endpoint not found")

    def _route_policy_bundles(self, parts: List[str], body: Dict):
        """Route policy bundle endpoints."""
        if len(parts) < 4:
            self.send_error_response(404, "not_found", "Endpoint not found")
            return

        bundle_name = parts[3]

        if len(parts) == 5 and parts[4] == 'versions':
            # POST /v1/policies/bundles/{bundle_name}/versions
            self.handle_create_policy_bundle(bundle_name, body)
            return

        if len(parts) == 6 and parts[4] == 'versions' and parts[5] == 'list':
            # POST /v1/policies/bundles/{bundle_name}/versions:list
            self.handle_list_policy_bundle_versions(bundle_name, body)
            return

        if len(parts) == 6 and parts[4] == 'versions':
            # Could be versions:list or versions/{version}:get
            action = parts[5]
            if action == 'list':
                self.handle_list_policy_bundle_versions(bundle_name, body)
                return
            # Check for :get suffix
            if action.endswith(':get'):
                try:
                    version = int(action[:-4])
                    self.handle_get_policy_bundle_version(bundle_name, version, body)
                except ValueError:
                    self.send_error_response(400, "bad_request", "Invalid version number")
                return
            self.send_error_response(404, "not_found", "Endpoint not found")
            return

        if len(parts) == 7 and parts[4] == 'versions' and parts[6] == 'get':
            try:
                version = int(parts[5])
                self.handle_get_policy_bundle_version(bundle_name, version, body)
            except ValueError:
                self.send_error_response(400, "bad_request", "Invalid version number")
            return

        # Handle versions/{version}:get pattern
        if len(parts) == 6 and parts[4] == 'versions':
            action = parts[5]
            if action.endswith(':get'):
                try:
                    version = int(action[:-4])
                    self.handle_get_policy_bundle_version(bundle_name, version, body)
                except ValueError:
                    self.send_error_response(400, "bad_request", "Invalid version number")
                return

        self.send_error_response(404, "not_found", "Endpoint not found")

    def _route_policy_bindings(self, parts: List[str], body: Dict):
        """Route policy binding endpoints."""
        # POST /v1/policies/bindings
        # parts = ['v1', 'policies', 'bindings']
        if len(parts) == 3:
            self.handle_create_policy_binding(body)
            return

        self.send_error_response(404, "not_found", "Endpoint not found")

    # -----------------------------------------------------------------------
    # Policy bundle handlers
    # -----------------------------------------------------------------------

    def handle_create_policy_bundle(self, bundle_name: str, body: Dict):
        """POST /v1/policies/bundles/{bundle_name}/versions"""
        rego_modules = None
        data = body.get("data", {})
        metadata = body.get("metadata", {})

        if "rego_modules" in body:
            rego_modules = body["rego_modules"]
            if not isinstance(rego_modules, dict):
                self.send_error_response(422, "policy_invalid",
                                        "rego_modules must be an object")
                return
        elif "tarball_b64" in body:
            # Decode tarball
            try:
                tarball_data = base64.b64decode(body["tarball_b64"])
                rego_modules, data = self._extract_tarball(tarball_data, data)
            except Exception as e:
                self.send_error_response(422, "policy_invalid",
                                        f"Failed to decode tarball: {e}")
                return
        else:
            self.send_error_response(422, "policy_invalid",
                                    "Missing rego_modules or tarball_b64")
            return

        # Validate entrypoints
        if not rego_modules:
            self.send_error_response(422, "policy_invalid",
                                    "No rego modules provided")
            return

        # Validate that modules can be parsed and have required entrypoints
        has_deny_or_warn = False
        for mod_name, mod_code in rego_modules.items():
            if not isinstance(mod_code, str):
                self.send_error_response(422, "policy_invalid",
                                        f"Module {mod_name} must be a string")
                return

            try:
                rules = RegoParser.parse(mod_code)
                for rule in rules:
                    if rule.get("kind") in ("deny", "warn"):
                        has_deny_or_warn = True
            except Exception as e:
                self.send_error_response(422, "policy_invalid",
                                        f"Failed to parse module {mod_name}: {e}")
                return

        if not has_deny_or_warn:
            self.send_error_response(422, "policy_invalid",
                                    "Bundle must contain at least one deny or warn rule")
            return

        # Check for forbidden patterns
        for mod_name, mod_code in rego_modules.items():
            if re.search(r'http\.(get|post|put|delete|patch)', mod_code):
                self.send_error_response(422, "policy_invalid",
                                        "Network I/O not allowed in policies")
                return
            if re.search(r'opa\.runtime', mod_code):
                self.send_error_response(422, "policy_invalid",
                                        "Runtime access not allowed in policies")
                return

        # Validate that entrypoints evaluate to arrays (basic check)
        try:
            test_input = {
                "target": {
                    "name": "",
                    "scope": {},
                    "version_used": 0,
                    "resolved_config": {},
                    "provenance": []
                },
                "graph": {"by_name": {}},
                "now": "2025-01-01T00:00:00Z"
            }
            result = RegoEngine.evaluate(rego_modules, test_input, data)
            if not isinstance(result.get("deny"), list):
                self.send_error_response(422, "policy_invalid",
                                        "data.guardrails.deny must evaluate to an array")
                return
            if not isinstance(result.get("warn"), list):
                self.send_error_response(422, "policy_invalid",
                                        "data.guardrails.warn must evaluate to an array")
                return
        except Exception as e:
            self.send_error_response(422, "policy_invalid",
                                    f"Bundle evaluation test failed: {e}")
            return

        # Create version
        try:
            version = self.policy_bundles.create_version(
                bundle_name, rego_modules, data, metadata
            )
        except ValueError as e:
            if "1 MiB" in str(e) or "size" in str(e).lower():
                self.send_error_response(413, "too_large", str(e))
            else:
                self.send_error_response(409, "policy_conflict", str(e))
            return

        self.send_json_response(201, {
            "bundle_name": bundle_name,
            "version": version
        })

    def _extract_tarball(self, tarball_data: bytes, default_data: Dict) -> Tuple[Dict, Dict]:
        """Extract rego modules and data from a tar.gz archive."""
        rego_modules = {}
        data = deepcopy(default_data) if default_data else {}

        with tarfile.open(fileobj=io.BytesIO(tarball_data), mode='r:gz') as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                f = tar.extractfile(member)
                if f is None:
                    continue
                content = f.read().decode('utf-8')
                name = member.name

                # Normalize path
                if name.startswith('./'):
                    name = name[2:]

                if name.endswith('.rego'):
                    # Use just the filename as key
                    key = os.path.basename(name)
                    rego_modules[key] = content
                elif name.endswith('.json') or name.endswith('.yaml') or name.endswith('.yml'):
                    key = os.path.basename(name)
                    # Try to parse as JSON/YAML and merge into data
                    try:
                        if name.endswith('.json'):
                            parsed = json.loads(content)
                        else:
                            parsed = yaml.safe_load(content)
                        if isinstance(parsed, dict):
                            data.update(parsed)
                    except Exception:
                        pass
                elif name == 'data.json':
                    try:
                        parsed = json.loads(content)
                        if isinstance(parsed, dict):
                            data.update(parsed)
                    except Exception:
                        pass

        return rego_modules, data

    def handle_list_policy_bundle_versions(self, bundle_name: str, body: Dict):
        """POST /v1/policies/bundles/{bundle_name}/versions:list"""
        versions = self.policy_bundles.get_versions_with_metadata(bundle_name)
        self.send_json_response(200, {
            "bundle_name": bundle_name,
            "versions": versions
        })

    def handle_get_policy_bundle_version(self, bundle_name: str, version: int, body: Dict):
        """POST /v1/policies/bundles/{bundle_name}/versions/{version}:get"""
        entry = self.policy_bundles.get_version(bundle_name, version)
        if entry is None:
            self.send_error_response(404, "policy_not_found", "Bundle version not found")
            return

        self.send_json_response(200, {
            "bundle_name": bundle_name,
            "version": version,
            "rego_modules": entry["rego_modules"],
            "data": entry.get("data", {}),
            "metadata": entry.get("metadata", {})
        })

    # -----------------------------------------------------------------------
    # Policy binding handlers
    # -----------------------------------------------------------------------

    def handle_create_policy_binding(self, body: Dict):
        """POST /v1/policies/bindings"""
        # Support both field name formats for flexibility
        bundle_ref = body.get("bundle", {})
        if not bundle_ref:
            # Alternative format: policy_bundle and policy_version as top-level fields
            bundle_name = body.get("policy_bundle")
            bundle_version = body.get("policy_version")
        else:
            bundle_name = bundle_ref.get("name")
            bundle_version = bundle_ref.get("version")

        # Support both 'selector' and 'selectors'
        selector = body.get("selector") or body.get("selectors")
        graph_keys = body.get("graph_keys", ["env", "tenant"])
        priority = body.get("priority", 0)

        # Validate bundle ref
        if not bundle_name or bundle_version is None:
            self.send_error_response(400, "invalid_input", "Missing bundle name/version")
            return

        # Check bundle exists
        if not self.policy_bundles.exists(bundle_name, bundle_version):
            self.send_error_response(404, "policy_not_found",
                                    "Bundle version not found")
            return

        # Validate selector
        if not selector or not isinstance(selector, dict) or len(selector) == 0:
            self.send_error_response(400, "invalid_input",
                                    "Selector must be a non-empty object")
            return

        for k, v in selector.items():
            if not isinstance(k, str) or not isinstance(v, (str, int, float, bool)):
                self.send_error_response(400, "invalid_input",
                                        "Selector values must be primitives")
                return

        # Validate graph_keys
        if not isinstance(graph_keys, list):
            self.send_error_response(400, "invalid_input",
                                    "graph_keys must be an array")
            return

        # Check for duplicate
        if self.policy_bindings.check_duplicate(bundle_name, bundle_version,
                                                selector, priority):
            self.send_error_response(409, "policy_conflict",
                                    "Duplicate binding for same bundle+selector+priority")
            return

        # Create binding
        try:
            binding_id, binding = self.policy_bindings.create_binding(
                bundle_name, bundle_version, selector, graph_keys, priority
            )
        except ValueError as e:
            self.send_error_response(409, "policy_conflict", str(e))
            return

        self.send_json_response(201, binding)

    # -----------------------------------------------------------------------
    # Policy stack/evaluate/explain handlers
    # -----------------------------------------------------------------------

    def handle_policy_stack(self, body: Dict):
        """POST /v1/policies/stack"""
        name = body.get("name")
        scope = body.get("scope", {})

        if not name:
            self.send_error_response(400, "invalid_input", "Missing name")
            return

        matching = self.policy_bindings.get_matching_bindings(name, scope)

        stack = []
        for b in matching:
            stack.append({
                "bundle": {"name": b["bundle"]["name"], "version": b["bundle"]["version"]},
                "selector": b["selector"],
                "graph_keys": b["graph_keys"],
                "priority": b["priority"]
            })

        self.send_json_response(200, stack)

    def handle_policy_evaluate(self, body: Dict):
        """POST /v1/policies/evaluate"""
        name = body.get("name")
        scope = body.get("scope", {})
        version = body.get("version")
        include_graph = body.get("include_graph", True)

        if not name:
            self.send_error_response(400, "invalid_input", "Missing name")
            return

        # Resolve version
        if version is None:
            version = self.configs.get_active_version(name, scope)
            if version is None:
                version = self.configs.get_latest_version(name, scope)

        if version is None:
            self.send_error_response(404, "not_found", "Config not found")
            return

        # Check config exists
        entry = self.configs.get_version(name, scope, version)
        if entry is None:
            self.send_error_response(404, "not_found", "Config version not found")
            return

        result, timed_out = evaluate_policies_for_target(
            self.configs, self.policy_bundles, self.policy_bindings,
            name, scope, version, include_graph
        )

        if timed_out:
            self.send_error_response(408, "evaluation_timeout",
                                    "Policy evaluation exceeded time budget",
                                    result if result.get("violations") else None)
            return

        self.send_json_response(200, result)

    def handle_policy_explain(self, body: Dict):
        """POST /v1/policies/explain"""
        violation_input = body.get("violation")
        if not violation_input:
            self.send_error_response(400, "invalid_input", "Missing violation")
            return

        policy_ref = violation_input.get("policy", {})
        target_ref = violation_input.get("target", {})
        rule_id = violation_input.get("rule_id", "")
        path = violation_input.get("path", "")

        policy_name = policy_ref.get("name", "")
        policy_version = policy_ref.get("version")
        target_name = target_ref.get("name", "")
        target_scope = target_ref.get("scope", {})

        # Build explanation
        lines = []

        # Selector info
        matching = self.policy_bindings.get_matching_bindings(target_name, target_scope)
        for b in matching:
            if b["bundle"]["name"] == policy_name and b["bundle"]["version"] == policy_version:
                selector_parts = [f"{k}={v}" for k, v in sorted(b["selector"].items())]
                lines.append(f"Selector matched: {' '.join(selector_parts)}")
                break

        # Resolved config value at path
        try:
            version = target_ref.get("version_used")
            if version is None:
                version = self.configs.get_active_version(target_name, target_scope)
            if version is None:
                version = self.configs.get_latest_version(target_name, target_scope)

            if version is not None:
                resolved, _ = self.configs.resolve_config(target_name, target_scope, version)
                scope_parts = [f"{v}" for v in sorted(target_scope.values())]
                scope_str = "/".join(scope_parts) if scope_parts else "default"

                if path:
                    try:
                        val = get_value_by_pointer(resolved, path)
                        lines.append(f"Resolved {target_name}@{scope_str} {path} = {canonical_json(val)}")
                    except (KeyError, ValueError):
                        lines.append(f"Resolved {target_name}@{scope_str} {path} = <not found>")
                else:
                    lines.append(f"Resolved {target_name}@{scope_str}")
        except (ValueError, KeyError):
            pass

        # Rule description
        bundle = self.policy_bundles.get_version(policy_name, policy_version)
        if bundle:
            # Try to find the rule and extract its message
            for mod_code in bundle["rego_modules"].values():
                try:
                    rules = RegoParser.parse(mod_code)
                    for rule in rules:
                        if rule.get("kind") in ("deny", "warn"):
                            # Try to extract the expected condition from the body
                            for line in rule.get("body", []):
                                line = line.strip()
                                # Look for comparison conditions
                                if rule_id and rule_id in line:
                                    lines.append(f"Rule {rule_id} triggered")
                                    break
                except Exception:
                    pass

        if not lines:
            lines.append(f"Rule {rule_id} triggered for {target_name} at {path}")

        lines.append(f"Decision: DENY (error)")

        self.send_json_response(200, {"explain": lines})

    # -----------------------------------------------------------------------
    # Schema handlers
    # -----------------------------------------------------------------------

    def handle_create_schema(self, schema_name: str, body: Dict):
        """Handle POST /v1/schemas/{schema_name}"""
        schema = None

        if "schema" in body:
            schema = body["schema"]
        elif "raw_schema" in body:
            raw = body["raw_schema"]
            fmt = body.get("raw_format", "json").lower()

            if fmt not in ("json", "yaml"):
                self.send_error_response(415, "unsupported_format",
                                        f"Unsupported format: {fmt}")
                return

            if len(raw.encode('utf-8')) > 1024 * 1024:
                self.send_error_response(413, "too_large", "Schema too large")
                return

            try:
                parsed = parse_raw_config(raw, fmt)
                if not isinstance(parsed, dict):
                    self.send_error_response(422, "schema_invalid",
                                            "Schema must be an object")
                    return
                schema = parsed
            except ParseError as e:
                self.send_error_response(422, "schema_invalid", str(e),
                                        {"reason": e.reason} if e.reason else None)
                return
        else:
            self.send_error_response(400, "bad_request",
                                    "Missing 'schema' or 'raw_schema' field")
            return

        try:
            check_external_refs(schema)
            jsonschema.Draft202012Validator.check_schema(schema)
        except ExternalRefError:
            self.send_error_response(422, "schema_invalid",
                                    "External $ref not allowed",
                                    {"reason": "external_ref_not_allowed"})
            return
        except jsonschema.exceptions.SchemaError as e:
            self.send_error_response(422, "schema_invalid", str(e))
            return
        except Exception as e:
            self.send_error_response(422, "schema_invalid", str(e))
            return

        try:
            version = self.schemas.create(schema_name, schema)
        except ValueError as e:
            self.send_error_response(409, "conflict", str(e))
            return

        self.send_json_response(201, {"name": schema_name, "version": version})

    def handle_list_schema_versions(self, schema_name: str, body: Dict):
        """Handle POST /v1/schemas/{schema_name}/versions"""
        versions = self.schemas.list_versions(schema_name)
        self.send_json_response(200, {"name": schema_name, "versions": versions})

    def handle_get_schema_version(self, schema_name: str, version: int, body: Dict):
        """Handle POST /v1/schemas/{schema_name}/{version}"""
        schema = self.schemas.get(schema_name, version)
        if schema is None:
            self.send_error_response(404, "not_found", "Schema version not found")
            return

        self.send_json_response(200, {
            "name": schema_name,
            "version": version,
            "schema": normalize_value(schema)
        })

    def handle_bind_schema(self, name: str, body: Dict):
        """Handle POST /v1/configs/{name}:bind"""
        scope = body.get("scope", {})
        schema_ref = body.get("schema_ref")

        if schema_ref is None:
            self.send_error_response(400, "bad_request", "Missing 'schema_ref'")
            return

        schema_name = schema_ref.get("name")
        schema_version = schema_ref.get("version")

        if not schema_name or schema_version is None:
            self.send_error_response(400, "bad_request", "Invalid 'schema_ref'")
            return

        if not self.schemas.exists(schema_name, schema_version):
            self.send_error_response(404, "not_found", "Schema version not found")
            return

        binding = self.configs.set_binding(name, scope, schema_ref)
        self.send_json_response(200, binding)

    def handle_get_binding(self, name: str, body: Dict):
        """Handle POST /v1/configs/{name}/schema"""
        scope = body.get("scope", {})
        binding = self.configs.get_binding(name, scope)

        if binding is None:
            self.send_error_response(404, "not_found", "No binding found")
            return

        response = {
            "name": binding["name"],
            "scope": binding["scope"],
            "schema_ref": binding["schema_ref"]
        }
        self.send_json_response(200, response)

    def handle_policy(self, name: str, body: Dict):
        scope = body.get("scope", {})

        if "required_approvals" not in body and "allow_author_approval" not in body and "allowed_reviewers" not in body:
            policy = self.proposals.get_policy(name, scope)
            self.send_json_response(200, policy)
            return

        required_approvals = body.get("required_approvals", 2)
        allow_author_approval = body.get("allow_author_approval", False)
        allowed_reviewers = body.get("allowed_reviewers")

        if not isinstance(required_approvals, int) or required_approvals < 1 or required_approvals > 10:
            self.send_error_response(422, "policy_violation",
                                    "required_approvals must be integer in [1, 10]")
            return

        if allowed_reviewers is not None:
            if not isinstance(allowed_reviewers, list):
                self.send_error_response(400, "bad_request", "allowed_reviewers must be a list or null")
                return
            for reviewer in allowed_reviewers:
                if not isinstance(reviewer, str) or len(reviewer.encode('utf-8')) > 128:
                    self.send_error_response(400, "bad_request", "Reviewer must be non-empty string <= 128 bytes")
                    return

        policy = self.proposals.set_policy(name, scope, required_approvals,
                                           allow_author_approval, allowed_reviewers)
        self.send_json_response(200, policy)

    def handle_create_config(self, name: str, body: Dict):
        """Handle POST /v1/configs/{name} - creates draft version."""
        scope = body.get("scope", {})
        includes = body.get("includes", [])
        schema_ref = body.get("schema_ref")

        config = None

        if "config" in body:
            config = body["config"]
        elif "raw_config" in body:
            raw = body["raw_config"]
            fmt = body.get("raw_format", "json").lower()

            if fmt not in ("json", "yaml", "toml"):
                self.send_error_response(415, "unsupported_format",
                                        f"Unsupported format: {fmt}")
                return

            if len(raw.encode('utf-8')) > 1024 * 1024:
                self.send_error_response(413, "too_large", "Config too large")
                return

            try:
                parsed = parse_raw_config(raw, fmt)
                if not isinstance(parsed, dict):
                    self.send_error_response(422, "unprocessable",
                                            "Config must be a JSON object")
                    return
                config = parsed
            except ParseError as e:
                details = {"reason": e.reason} if e.reason else None
                self.send_error_response(422, "unprocessable", str(e), details)
                return
        else:
            self.send_error_response(400, "bad_request",
                                    "Missing 'config' or 'raw_config' field")
            return

        config = normalize_value(config)

        effective_schema_ref, effective_schema = self.resolve_effective_schema(name, scope, schema_ref)
        if effective_schema_ref is None and schema_ref is not None:
            return

        if effective_schema:
            try:
                validate_config_against_schema(config, effective_schema)
            except SchemaValidationError as e:
                self.send_validation_error(e)
                return

        version = self.configs.create_version(name, scope, config, includes, schema_ref)

        self.send_json_response(201, {
            "name": name,
            "scope": scope,
            "version": version,
            "status": "draft",
            "active": False
        })

    def handle_list_config_versions(self, name: str, body: Dict):
        """Handle POST /v1/configs/{name}/versions"""
        scope = body.get("scope", {})

        scope_key = self.configs.scope_to_key(scope)
        full_key = (name, scope_key)

        if full_key not in self.configs.configs:
            self.send_json_response(200, {"name": name, "scope": scope, "versions": []})
            return

        versions = sorted(self.configs.configs[full_key].keys())
        self.send_json_response(200, {"name": name, "scope": scope, "versions": versions})

    def handle_get_config_version(self, name: str, version: int, body: Dict):
        """Handle POST /v1/configs/{name}/{version}"""
        scope = body.get("scope", {})

        entry = self.configs.get_version(name, scope, version)
        if entry is None:
            self.send_error_response(404, "not_found", "Config version not found")
            return

        status = self.configs.get_version_status(name, scope, version)
        active_version = self.configs.get_active_version(name, scope)
        is_active = (active_version == version)

        self.send_json_response(200, {
            "name": name,
            "scope": scope,
            "version": version,
            "config": entry["config"],
            "includes": entry.get("includes", []),
            "status": status,
            "active": is_active
        })

    def handle_resolve_config(self, name: str, body: Dict):
        """Handle POST /v1/configs/{name}:resolve"""
        scope = body.get("scope", {})
        version = body.get("version")
        schema_ref = body.get("schema_ref")

        latest = self.configs.get_latest_version(name, scope)
        if latest is None:
            self.send_error_response(404, "not_found", "Config not found")
            return

        try:
            resolved_config, inheritance_chain = self.configs.resolve_config(
                name, scope, version
            )
        except ValueError as e:
            self.send_error_response(400, "bad_request", str(e))
            return

        effective_schema_ref, effective_schema = self.resolve_effective_schema(name, scope, schema_ref)
        if effective_schema_ref is None and schema_ref is not None:
            return

        response = {
            "name": name,
            "scope": scope,
            "resolved_config": normalize_value(resolved_config),
            "inheritance_chain": inheritance_chain
        }

        if effective_schema:
            try:
                validate_config_against_schema(resolved_config, effective_schema)
                response["validated_against"] = effective_schema_ref
            except SchemaValidationError as e:
                self.send_validation_error(e)
                return

        self.send_json_response(200, response)

    def handle_validate_config(self, name: str, body: Dict):
        scope = body.get("scope", {})
        version = body.get("version")
        schema_ref = body.get("schema_ref")
        mode = body.get("mode", "resolved")

        if mode not in ("stored", "resolved"):
            self.send_error_response(400, "bad_request", "Invalid mode")
            return

        latest = self.configs.get_latest_version(name, scope)
        if latest is None:
            self.send_error_response(404, "not_found", "Config not found")
            return

        version_used = version or latest

        entry = self.configs.get_version(name, scope, version_used)
        if entry is None:
            self.send_error_response(404, "not_found", "Config version not found")
            return

        effective_schema_ref, effective_schema = self.resolve_effective_schema(name, scope, schema_ref)
        if effective_schema_ref is None and schema_ref is not None:
            return

        if effective_schema is None:
            self.send_error_response(404, "schema_not_bound", "No schema bound")
            return

        if mode == "stored":
            config_to_validate = entry["config"]
        else:
            try:
                config_to_validate, _ = self.configs.resolve_config(name, scope, version_used)
            except ValueError as e:
                self.send_error_response(400, "bad_request", str(e))
                return

        try:
            validate_config_against_schema(config_to_validate, effective_schema)
            self.send_json_response(200, {
                "name": name,
                "scope": scope,
                "version_used": version_used,
                "mode": mode,
                "valid": True,
                "validated_against": effective_schema_ref
            })
        except SchemaValidationError as e:
            self.send_validation_error(e)

    def handle_propose(self, name: str, version: int, body: Dict):
        """Handle POST /v1/configs/{name}/{version}:propose"""
        scope = body.get("scope", {})
        author = body.get("author")
        title = body.get("title")
        description = body.get("description")
        base_version = body.get("base_version")
        labels = body.get("labels", [])

        # Validate required fields
        if not author:
            self.send_error_response(400, "bad_request", "Missing 'author'")
            return

        if base_version is None:
            self.send_error_response(400, "bad_request", "Missing 'base_version'")
            return

        # Validate author
        if not isinstance(author, str) or len(author.encode('utf-8')) > 128 or not author:
            self.send_error_response(400, "bad_request", "Invalid author")
            return

        # Validate title
        if title is not None:
            if not isinstance(title, str) or len(title.encode('utf-8')) > 200:
                self.send_error_response(400, "bad_request", "title must be <= 200 bytes")
                return

        # Validate description
        if description is not None:
            if not isinstance(description, str) or len(description.encode('utf-8')) > 8192:
                self.send_error_response(400, "bad_request", "description must be <= 8 KiB")
                return

        # Validate labels
        if labels:
            if not isinstance(labels, list) or len(labels) > 32:
                self.send_error_response(400, "bad_request", "labels must be <= 32 items")
                return
            for label in labels:
                if not isinstance(label, str) or len(label.encode('utf-8')) > 32 or \
                   not re.match(r'^[a-z0-9._-]+$', label):
                    self.send_error_response(400, "bad_request",
                                            "label must match [a-z0-9._-]+ and be <= 32 bytes")
                    return

        # Check draft version exists and is for this name/scope
        draft_entry = self.configs.get_version(name, scope, version)
        if draft_entry is None:
            self.send_error_response(409, "conflict", "Draft version not found")
            return

        # Check version is still a draft
        version_status = self.configs.get_version_status(name, scope, version)
        if version_status != "draft":
            self.send_error_response(409, "conflict", "Version is not a draft")
            return

        # Check base_version matches current active version
        current_active = self.configs.get_active_version(name, scope)
        if base_version != current_active:
            self.send_error_response(409, "stale_base",
                                    f"base_version {base_version} does not match current active {current_active}")
            return

        # Check proposal limit
        proposal_count = self.proposals.count_proposals_for_identity(name, scope)
        if proposal_count >= 1000:
            self.send_error_response(409, "conflict", "Maximum proposals exceeded")
            return

        # Validate stored config against schema
        effective_schema_ref, effective_schema = self.resolve_effective_schema(
            name, scope, draft_entry.get("schema_ref")
        )
        if effective_schema_ref is None and draft_entry.get("schema_ref") is not None:
            return

        if effective_schema:
            try:
                validate_config_against_schema(draft_entry["config"], effective_schema)
            except SchemaValidationError as e:
                self.send_validation_error(e)
                return

        # Validate resolved config against schema
        try:
            resolved_config, _ = self.configs.resolve_config(name, scope, version)
        except ValueError as e:
            self.send_error_response(400, "bad_request", str(e))
            return

        if effective_schema:
            try:
                validate_config_against_schema(resolved_config, effective_schema)
            except SchemaValidationError as e:
                self.send_validation_error(e)
                return

        # ---- Policy evaluation (Checkpoint 4) ----
        policy_result, timed_out = evaluate_policies_for_target(
            self.configs, self.policy_bundles, self.policy_bindings,
            name, scope, version
        )

        if timed_out:
            self.send_error_response(408, "evaluation_timeout",
                                    "Policy evaluation exceeded time budget",
                                    policy_result)
            return

        # Check for error violations
        has_errors = policy_result["tally"]["errors"] > 0
        if has_errors:
            self.send_error_response(422, "policy_violation",
                                    "Proposal blocked by policy violations",
                                    policy_result)
            return

        # Get policy snapshot
        policy = self.proposals.get_policy(name, scope)

        # Compute diffs
        base_entry = self.configs.get_version(name, scope, base_version) if base_version else None

        if base_entry:
            base_config = base_entry["config"]
            base_includes = base_entry.get("includes", [])
            try:
                base_resolved, _ = self.configs.resolve_config(name, scope, base_version)
            except ValueError:
                base_resolved = {}
        else:
            base_config = {}
            base_includes = []
            base_resolved = {}

        diffs = compute_diffs(
            base_config, draft_entry["config"],
            base_resolved, resolved_config,
            base_includes, draft_entry.get("includes", [])
        )

        # Create proposal with policy_summary
        proposal_id = self.proposals.create_proposal(
            name=name,
            scope=scope,
            draft_version=version,
            base_version=base_version,
            author=author,
            title=title,
            description=description,
            labels=labels,
            quorum=policy,
            diffs=diffs,
            policy_summary=policy_result
        )

        # Supersede previous open proposals for this draft
        open_proposals = self.proposals.get_open_proposals_for_draft(name, scope, version)
        for p in open_proposals:
            if p["proposal_id"] != proposal_id:
                self.proposals.update_proposal(p["proposal_id"], {"status": "superseded"})

        proposal = self.proposals.get_proposal(proposal_id)
        self.send_json_response(201, self._format_proposal(proposal))

    def _format_proposal(self, proposal: Dict) -> Dict:
        """Format a proposal for response."""
        result = {
            "proposal_id": proposal["proposal_id"],
            "name": proposal["name"],
            "scope": proposal["scope"],
            "draft_version": proposal["draft_version"],
            "base_version": proposal["base_version"],
            "author": proposal["author"],
            "title": proposal.get("title"),
            "description": proposal.get("description"),
            "labels": proposal.get("labels", []),
            "quorum": proposal["quorum"],
            "status": proposal["status"],
            "tally": {
                "approvals": proposal["tally"]["approvals"],
                "rejections": proposal["tally"]["rejections"],
                "by_actor": dict(sorted(proposal["tally"]["by_actor"].items()))
            },
            "diffs": proposal["diffs"]
        }
        if proposal.get("policy_summary") is not None:
            result["policy_summary"] = proposal["policy_summary"]
        return result

    def handle_list_proposals(self, name: str, body: Dict):
        """Handle POST /v1/configs/{name}/proposals:list"""
        scope = body.get("scope", {})
        status = body.get("status", "any")

        valid_statuses = ["open", "approved", "rejected", "merged", "withdrawn", "superseded", "any"]
        if status not in valid_statuses:
            self.send_error_response(400, "bad_request", f"Invalid status: {status}")
            return

        proposals = self.proposals.list_proposals(name, scope, status if status != "any" else None)
        formatted = [self._format_proposal(p) for p in proposals]

        self.send_json_response(200, {"proposals": formatted})

    def handle_get_proposal(self, proposal_id: int, body: Dict):
        """Handle POST /v1/proposals/{proposal_id}:get"""
        proposal = self.proposals.get_proposal(proposal_id)
        if proposal is None:
            self.send_error_response(404, "not_found", "Proposal not found")
            return

        self.send_json_response(200, self._format_proposal(proposal))

    def handle_review(self, proposal_id: int, body: Dict):
        """Handle POST /v1/proposals/{proposal_id}:review"""
        proposal = self.proposals.get_proposal(proposal_id)
        if proposal is None:
            self.send_error_response(404, "not_found", "Proposal not found")
            return

        # Check if proposal is closed
        if proposal["status"] in ("merged", "withdrawn", "superseded"):
            # Return existing state without changes
            self.send_json_response(200, self._format_proposal(proposal))
            return

        actor = body.get("actor")
        decision = body.get("decision")
        message = body.get("message")

        # Validate required fields
        if not actor:
            self.send_error_response(400, "bad_request", "Missing 'actor'")
            return

        if decision not in ("approve", "reject"):
            self.send_error_response(400, "bad_request", "decision must be 'approve' or 'reject'")
            return

        # Validate actor
        if not isinstance(actor, str) or len(actor.encode('utf-8')) > 128 or not actor:
            self.send_error_response(400, "bad_request", "Invalid actor")
            return

        # Check review limit
        review_count = self.proposals.count_reviews_for_proposal(proposal_id)
        if review_count >= 1000 and actor not in proposal["tally"]["by_actor"]:
            self.send_error_response(409, "conflict", "Maximum reviews exceeded")
            return

        # Check policy
        quorum = proposal["quorum"]
        allowed_reviewers = quorum.get("allowed_reviewers")
        allow_author_approval = quorum.get("allow_author_approval", False)
        author = proposal["author"]

        # Check if actor is allowed
        if allowed_reviewers is not None and actor not in allowed_reviewers:
            self.send_error_response(422, "policy_violation", "Actor not in allowed_reviewers")
            return

        # Check author approval
        if decision == "approve" and actor == author and not allow_author_approval:
            self.send_error_response(422, "policy_violation", "Author cannot approve own proposal")
            return

        # Check idempotency
        current_review = proposal["tally"]["by_actor"].get(actor)
        if current_review and current_review.get("decision") == decision and current_review.get("message") == message:
            # Idempotent - return same state
            self.send_json_response(200, self._format_proposal(proposal))
            return

        # Add review
        self.proposals.add_review(proposal_id, actor, decision, message)

        proposal = self.proposals.get_proposal(proposal_id)
        self.send_json_response(200, self._format_proposal(proposal))

    def handle_merge(self, proposal_id: int, body: Dict = None):
        """Handle POST /v1/proposals/{proposal_id}:merge"""
        if body is None:
            body = {}

        proposal = self.proposals.get_proposal(proposal_id)
        if proposal is None:
            self.send_error_response(404, "not_found", "Proposal not found")
            return

        # Check if already merged
        if proposal["status"] == "merged":
            # Idempotent - return same response
            self.send_json_response(200, {
                "activated_version": proposal["draft_version"],
                "previous_active": proposal["base_version"],
                "proposal_id": proposal_id
            })
            return

        # Check if proposal is closed
        if proposal["status"] in ("withdrawn", "superseded"):
            self.send_error_response(409, "conflict", "Proposal is closed")
            return

        # Check if approved
        if proposal["status"] != "approved":
            self.send_error_response(409, "conflict", "Proposal is not approved")
            return

        name = proposal["name"]
        scope = proposal["scope"]
        draft_version = proposal["draft_version"]
        base_version = proposal["base_version"]

        # Check base version still matches
        current_active = self.configs.get_active_version(name, scope)
        if current_active != base_version:
            self.send_error_response(409, "stale_base",
                                    "Base version has changed since proposal was created")
            return

        # Check draft version still exists and is draft
        draft_entry = self.configs.get_version(name, scope, draft_version)
        if draft_entry is None:
            self.send_error_response(409, "conflict", "Draft version not found")
            return

        # Revalidate resolved config against schema
        effective_schema_ref, effective_schema = self.resolve_effective_schema(
            name, scope, draft_entry.get("schema_ref")
        )

        try:
            resolved_config, _ = self.configs.resolve_config(name, scope, draft_version)
        except ValueError as e:
            self.send_error_response(409, "not_mergeable", str(e))
            return

        if effective_schema:
            try:
                validate_config_against_schema(resolved_config, effective_schema)
            except SchemaValidationError as e:
                self.send_error_response(409, "not_mergeable", "Schema validation failed")
                return

        # ---- Re-run policy evaluation before merge (Checkpoint 4) ----
        policy_result, timed_out = evaluate_policies_for_target(
            self.configs, self.policy_bundles, self.policy_bindings,
            name, scope, draft_version
        )

        if timed_out:
            self.send_error_response(408, "evaluation_timeout",
                                    "Policy evaluation exceeded time budget",
                                    policy_result)
            return

        has_errors = policy_result["tally"]["errors"] > 0
        if has_errors:
            self.send_error_response(409, "not_mergeable",
                                    "Merge blocked by policy violations",
                                    policy_result)
            return

        # Activate the draft
        self.configs.set_active_version(name, scope, draft_version)

        # Update proposal status
        self.proposals.update_proposal(proposal_id, {"status": "merged"})

        # Supersede other open proposals for this identity
        open_proposals = self.proposals.get_open_proposals_for_identity(name, scope)
        for p in open_proposals:
            if p["proposal_id"] != proposal_id:
                self.proposals.update_proposal(p["proposal_id"], {"status": "superseded"})

        self.send_json_response(200, {
            "activated_version": draft_version,
            "previous_active": base_version,
            "proposal_id": proposal_id
        })

    def handle_withdraw(self, proposal_id: int, body: Dict):
        """Handle POST /v1/proposals/{proposal_id}:withdraw"""
        proposal = self.proposals.get_proposal(proposal_id)
        if proposal is None:
            self.send_error_response(404, "not_found", "Proposal not found")
            return

        # Check if proposal is closed
        if proposal["status"] in ("merged", "withdrawn", "superseded"):
            # Return existing state without changes
            self.send_json_response(200, self._format_proposal(proposal))
            return

        actor = body.get("actor")
        reason = body.get("reason")

        # Validate actor
        if not actor:
            self.send_error_response(400, "bad_request", "Missing 'actor'")
            return

        # Check actor is author
        if actor != proposal["author"]:
            self.send_error_response(422, "policy_violation", "Only author can withdraw")
            return

        # Withdraw
        self.proposals.update_proposal(proposal_id, {"status": "withdrawn"})

        proposal = self.proposals.get_proposal(proposal_id)
        self.send_json_response(200, self._format_proposal(proposal))

    def handle_activate(self, name: str, version: int, body: Dict):
        """Handle POST /v1/configs/{name}/versions/{version}:activate"""
        scope = body.get("scope", {})
        proposal_id = body.get("proposal_id")

        if proposal_id is None:
            self.send_error_response(409, "approval_required", "Proposal required for activation")
            return

        proposal = self.proposals.get_proposal(proposal_id)
        if proposal is None:
            self.send_error_response(409, "approval_required", "Proposal not found")
            return

        # Check proposal matches the target
        if proposal["name"] != name or proposal["draft_version"] != version:
            self.send_error_response(409, "approval_required", "Proposal does not match target")
            return

        scope_key = self.configs.scope_to_key(scope)
        proposal_scope_key = self.configs.scope_to_key(proposal["scope"])
        if scope_key != proposal_scope_key:
            self.send_error_response(409, "approval_required", "Proposal scope does not match")
            return

        # Check if approved
        if proposal["status"] != "approved":
            self.send_error_response(409, "approval_required", "Proposal is not approved")
            return

        # Check base version
        current_active = self.configs.get_active_version(name, scope)
        if current_active != proposal["base_version"]:
            self.send_error_response(409, "stale_base", "Base version has changed")
            return

        # Proceed with merge
        self.handle_merge(proposal_id, body)

    def handle_rollback(self, name: str, body: Dict):
        """Handle POST /v1/configs/{name}:rollback"""
        scope = body.get("scope", {})
        proposal_id = body.get("proposal_id")

        if proposal_id is None:
            self.send_error_response(409, "approval_required", "Proposal required for rollback")
            return

        proposal = self.proposals.get_proposal(proposal_id)
        if proposal is None:
            self.send_error_response(409, "approval_required", "Proposal not found")
            return

        # Check proposal matches the target (rollback targets base_version)
        if proposal["name"] != name:
            self.send_error_response(409, "approval_required", "Proposal does not match target")
            return

        scope_key = self.configs.scope_to_key(scope)
        proposal_scope_key = self.configs.scope_to_key(proposal["scope"])
        if scope_key != proposal_scope_key:
            self.send_error_response(409, "approval_required", "Proposal scope does not match")
            return

        # Check if approved
        if proposal["status"] not in ("approved", "merged"):
            self.send_error_response(409, "approval_required", "Proposal is not approved")
            return

        # For rollback, we need a proposal targeting the version we want to rollback to
        # The proposal's draft_version becomes active
        # Check base version
        current_active = self.configs.get_active_version(name, scope)
        if current_active != proposal["base_version"]:
            self.send_error_response(409, "stale_base", "Base version has changed")
            return

        # If already merged, return idempotent response
        if proposal["status"] == "merged":
            self.send_json_response(200, {
                "activated_version": proposal["draft_version"],
                "previous_active": proposal["base_version"],
                "proposal_id": proposal_id
            })
            return

        # Proceed with merge
        self.handle_merge(proposal_id, body)


def run_server(address: str = "0.0.0.0", port: int = 8080):
    ConfigServerHandler.schemas = SchemaRegistry()
    ConfigServerHandler.configs = ConfigStore()
    ConfigServerHandler.proposals = ProposalStore()
    ConfigServerHandler.policy_bundles = PolicyBundleStore()
    ConfigServerHandler.policy_bindings = PolicyBindingStore()

    server = HTTPServer((address, port), ConfigServerHandler)
    print(f"Config server running on {address}:{port}", file=sys.stderr)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...", file=sys.stderr)
        server.shutdown()


def main():
    parser = argparse.ArgumentParser(description="Config Server")
    parser.add_argument("--address", default="0.0.0.0", help="Address to bind to")
    parser.add_argument("--port", type=int, default=8080, help="Port to bind to")
    args = parser.parse_args()

    run_server(args.address, args.port)


if __name__ == "__main__":
    main()
