#!/usr/bin/env python3
"""Config Server - Configuration management with schema registry, workflow, and policy guardrails."""

import argparse
import base64
import io
import json
import os
import re
import sys
import tarfile
import time
import traceback
from copy import deepcopy
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import jsonschema
import yaml

from parsers import parse_raw_config, ParseError, normalize_value, canonical_json
from schema_validation import (
    SchemaValidationError, validate_config_against_schema,
    check_external_refs, get_json_type
)
from json_utils import (
    get_value_by_pointer, compute_diffs, canonical_json as _canonical_json
)
from stores import (
    SchemaRegistry, ConfigStore, ProposalStore,
    PolicyBundleStore, PolicyBindingStore
)
from rego_engine import RegoEngine, RegoParser


# --- Policy evaluation ---

def _now_rfc3339():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _build_policy_input(target_name, target_scope, target_version,
                        target_resolved, target_inheritance, graph_by_name, now):
    return {
        "target": {
            "name": target_name,
            "scope": normalize_value(target_scope),
            "version_used": target_version,
            "resolved_config": normalize_value(target_resolved),
            "provenance": target_inheritance
        },
        "graph": {"by_name": graph_by_name},
        "now": now
    }


def _gather_graph(config_store, target_name, target_scope, graph_keys,
                  target_version, target_resolved, target_inheritance):
    target_scope_normalized = normalize_value(target_scope)
    graph_filter = {k: target_scope_normalized[k] for k in graph_keys if k in target_scope_normalized}

    if not graph_filter:
        return {target_name: normalize_value(target_resolved)}, False

    candidates = []
    for (name, scope_key), versions in config_store.configs.items():
        scope = config_store.key_to_scope(scope_key)
        scope_normalized = normalize_value(scope)
        if all(scope_normalized.get(k) == v for k, v in graph_filter.items()):
            active_v = config_store.get_active_version(name, scope) or max(versions.keys())
            candidates.append((name, scope, active_v))

    # Ensure target is included
    target_entry = (target_name, target_scope, target_version)
    if not any(c[0] == target_name and
               config_store.scope_to_key(c[1]) == config_store.scope_to_key(target_scope)
               for c in candidates):
        candidates.append(target_entry)

    candidates.sort(key=lambda x: x[0])
    truncated = len(candidates) > 2000
    if truncated:
        candidates = candidates[:2000]

    by_name = {}
    for name, scope, version in candidates:
        try:
            resolved, _ = config_store.resolve_config(name, scope, version)
            by_name[name] = normalize_value(resolved)
        except (ValueError, KeyError):
            entry = config_store.get_version(name, scope, version)
            if entry:
                by_name[name] = normalize_value(entry["config"])

    return dict(sorted(by_name.items())), truncated


def evaluate_policies_for_target(config_store, bundle_store, binding_store,
                                  target_name, target_scope, target_version=None,
                                  include_graph=True):
    try:
        target_resolved, target_inheritance = config_store.resolve_config(
            target_name, target_scope, target_version
        )
    except (ValueError, KeyError):
        return {"policy_stack": [], "violations": [], "tally": {"errors": 0, "warnings": 0}}, False

    if target_version is None:
        target_version = config_store.get_active_version(target_name, target_scope)
        if target_version is None:
            target_version = config_store.get_latest_version(target_name, target_scope)

    matching_bindings = binding_store.get_matching_bindings(target_name, target_scope)
    if not matching_bindings:
        return {"policy_stack": [], "violations": [], "tally": {"errors": 0, "warnings": 0}}, False

    policy_stack = [{
        "bundle": {"name": b["bundle"]["name"], "version": b["bundle"]["version"]},
        "selector": b["selector"],
        "graph_keys": b["graph_keys"],
        "priority": b["priority"]
    } for b in matching_bindings]

    all_graph_keys = sorted(set(k for b in matching_bindings for k in b["graph_keys"]))

    if include_graph:
        by_name, graph_truncated = _gather_graph(
            config_store, target_name, target_scope, all_graph_keys,
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
        elapsed_ms = (time.monotonic() - start_time) * 1000
        if elapsed_ms > 500:
            timed_out = True
            break

        bundle = bundle_store.get_version(b["bundle"]["name"], b["bundle"]["version"])
        if bundle is None:
            continue

        input_data = _build_policy_input(
            target_name, target_scope, target_version,
            normalize_value(target_resolved), target_inheritance, by_name, now
        )

        try:
            rego_result = RegoEngine.evaluate(
                bundle["rego_modules"], input_data, bundle.get("data", {}),
                timeout_ms=max(1, int(500 - elapsed_ms))
            )
        except Exception:
            rego_result = {"deny": [], "warn": []}

        for severity in ("deny", "warn"):
            for v in rego_result.get(severity, []):
                if len(all_violations) >= 1000:
                    truncated = True
                    break
                if not isinstance(v, dict):
                    continue
                violation = {
                    "policy": {"name": b["bundle"]["name"], "version": b["bundle"]["version"]},
                    "target": {
                        "name": target_name,
                        "scope": normalize_value(target_scope),
                        "version_used": target_version
                    },
                    "rule_id": v.get("id", ""),
                    "severity": "error" if severity == "deny" else "warn",
                    "path": v.get("path", ""),
                    "message": v.get("message", ""),
                }
                evidence = {k: val for k, val in v.items() if k not in ("id", "message", "path", "target")}
                if evidence:
                    violation["evidence"] = evidence
                all_violations.append(violation)
            if truncated:
                break
        if truncated:
            break

    all_violations.sort(key=lambda v: (
        v["target"]["name"], v["policy"]["name"], v["policy"]["version"],
        v["rule_id"], v["path"]
    ))

    result = {
        "policy_stack": policy_stack,
        "violations": all_violations,
        "tally": {
            "errors": sum(1 for v in all_violations if v["severity"] == "error"),
            "warnings": sum(1 for v in all_violations if v["severity"] == "warn")
        }
    }
    if truncated:
        result["truncated"] = True
    if graph_truncated:
        result["details"] = {"graph_truncated": True}

    return result, timed_out


# --- HTTP Handler ---

class ConfigServerHandler(BaseHTTPRequestHandler):
    schemas: SchemaRegistry = None
    configs: ConfigStore = None
    proposals: ProposalStore = None
    policy_bundles: PolicyBundleStore = None
    policy_bindings: PolicyBindingStore = None

    def log_message(self, format, *args):
        pass

    def send_json(self, status, data):
        body = json.dumps(data, separators=(',', ':')).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status, code, message, details=None):
        error = {"code": code, "message": message}
        if details:
            error["details"] = details
        self.send_json(status, {"error": error})

    def send_validation_error(self, e):
        self.send_error_json(422, "validation_failed", "Config does not conform to schema", {
            "path": e.path, "rule": e.rule, "expected": e.expected, "actual": e.actual
        })

    def read_body(self):
        length = int(self.headers.get('Content-Length', 0))
        if length == 0:
            return b'{}'
        if length > 1024 * 1024:
            return None
        return self.rfile.read(length)

    def parse_body(self):
        body = self.read_body()
        if body is None:
            return None, "too_large"
        try:
            return json.loads(body.decode('utf-8')), None
        except json.JSONDecodeError:
            return None, "invalid_json"

    def parse_path(self):
        parsed = urlparse(self.path)
        return parsed.path, [p for p in parsed.path.split('/') if p], parse_qs(parsed.query)

    def do_GET(self):
        if self.path == '/healthz':
            self.send_json(200, {"ok": True})
        else:
            self.send_error_json(404, "not_found", "Endpoint not found")

    def do_POST(self):
        path, parts, _ = self.parse_path()

        if path == '/healthz':
            self.send_json(200, {"ok": True})
            return

        if not self.headers.get('Content-Type', '').startswith('application/json'):
            self.send_error_json(415, "unsupported_media_type", "Content-Type must be application/json")
            return

        body, error = self.parse_body()
        if error == "too_large":
            self.send_error_json(413, "too_large", "Request body too large")
            return
        if error == "invalid_json":
            self.send_error_json(400, "bad_request", "Invalid JSON body")
            return

        body = body or {}

        try:
            self.route(parts, body)
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            self.send_error_json(500, "internal_error", str(e))

    def route(self, parts, body):
        if len(parts) < 2 or parts[0] != 'v1':
            self.send_error_json(404, "not_found", "Endpoint not found")
            return

        resource = parts[1]
        if resource == 'schemas':
            self.route_schemas(parts, body)
        elif resource == 'configs':
            self.route_configs(parts, body)
        elif resource == 'proposals':
            self.route_proposals(parts, body)
        elif resource == 'policies':
            self.route_policies(parts, body)
        else:
            self.send_error_json(404, "not_found", "Endpoint not found")

    # --- Schema routes ---

    def route_schemas(self, parts, body):
        if len(parts) < 3:
            self.send_error_json(404, "not_found", "Endpoint not found")
            return
        name = parts[2]

        if len(parts) == 3:
            self.create_schema(name, body)
        elif len(parts) == 4 and parts[3] == 'versions':
            self.list_schema_versions(name, body)
        elif len(parts) == 4:
            try:
                version = int(parts[3])
                self.get_schema_version(name, version, body)
            except ValueError:
                self.send_error_json(400, "bad_request", "Invalid version number")
        else:
            self.send_error_json(404, "not_found", "Endpoint not found")

    def create_schema(self, name, body):
        if "schema" in body:
            schema = body["schema"]
        elif "raw_schema" in body:
            raw = body["raw_schema"]
            fmt = body.get("raw_format", "json").lower()
            if fmt not in ("json", "yaml"):
                self.send_error_json(415, "unsupported_format", f"Unsupported format: {fmt}")
                return
            if len(raw.encode('utf-8')) > 1024 * 1024:
                self.send_error_json(413, "too_large", "Schema too large")
                return
            try:
                parsed = parse_raw_config(raw, fmt)
                if not isinstance(parsed, dict):
                    self.send_error_json(422, "schema_invalid", "Schema must be an object")
                    return
                schema = parsed
            except ParseError as e:
                self.send_error_json(422, "schema_invalid", str(e),
                                      {"reason": e.reason} if e.reason else None)
                return
        else:
            self.send_error_json(400, "bad_request", "Missing 'schema' or 'raw_schema' field")
            return

        try:
            check_external_refs(schema)
            jsonschema.Draft202012Validator.check_schema(schema)
        except Exception as e:
            self.send_error_json(422, "schema_invalid", str(e))
            return

        try:
            version = self.schemas.create(name, schema)
        except ValueError as e:
            self.send_error_json(409, "conflict", str(e))
            return

        self.send_json(201, {"name": name, "version": version})

    def list_schema_versions(self, name, body):
        self.send_json(200, {"name": name, "versions": self.schemas.list_versions(name)})

    def get_schema_version(self, name, version, body):
        schema = self.schemas.get(name, version)
        if schema is None:
            self.send_error_json(404, "not_found", "Schema version not found")
            return
        self.send_json(200, {"name": name, "version": version, "schema": normalize_value(schema)})

    # --- Config routes ---

    def route_configs(self, parts, body):
        if len(parts) < 3:
            self.send_error_json(404, "not_found", "Endpoint not found")
            return
        name = parts[2]

        if len(parts) == 3:
            self._route_config_name(name, body)
        elif len(parts) >= 4:
            self._route_config_version(name, parts, body)

    def _route_config_name(self, name, body):
        if name.endswith(':policy'):
            self.get_set_policy(name[:-7], body)
        elif name.endswith(':bind'):
            self.bind_schema(name[:-5], body)
        elif name.endswith(':resolve'):
            self.resolve_config(name[:-8], body)
        elif name.endswith(':validate'):
            self.validate_config(name[:-9], body)
        elif name.endswith(':rollback'):
            self.rollback(name[:-9], body)
        else:
            self.create_config(name, body)

    def _route_config_version(self, name, parts, body):
        segment = parts[3]

        if segment.endswith(':propose'):
            try:
                self.propose(name, int(segment[:-8]), body)
            except ValueError:
                self.send_error_json(400, "bad_request", "Invalid version number")
            return

        if segment.endswith(':activate'):
            try:
                self.activate(name, int(segment[:-9]), body)
            except ValueError:
                self.send_error_json(400, "bad_request", "Invalid version number")
            return

        if segment == 'versions':
            self.list_config_versions(name, body)
            return

        if segment == 'proposals:list':
            self.list_proposals(name, body)
            return

        try:
            version = int(segment)
            if len(parts) == 4:
                self.get_config_version(name, version, body)
            elif len(parts) == 5 and parts[4] == 'schema':
                self.get_binding(name, body)
            else:
                self.send_error_json(404, "not_found", "Endpoint not found")
        except ValueError:
            self.send_error_json(400, "bad_request", "Invalid version number")

    def resolve_effective_schema(self, name, scope, schema_ref=None):
        if schema_ref:
            schema_name = schema_ref.get("name")
            schema_version = schema_ref.get("version")
            if not self.schemas.exists(schema_name, schema_version):
                self.send_error_json(404, "not_found", "Schema version not found")
                return None, None
            return schema_ref, self.schemas.get(schema_name, schema_version)
        binding = self.configs.get_binding(name, scope)
        if binding:
            ref = binding["schema_ref"]
            return ref, self.schemas.get(ref.get("name"), ref.get("version"))
        return None, None

    def create_config(self, name, body):
        scope = body.get("scope", {})
        includes = body.get("includes", [])
        schema_ref = body.get("schema_ref")

        if "config" in body:
            config = body["config"]
        elif "raw_config" in body:
            raw = body["raw_config"]
            fmt = body.get("raw_format", "json").lower()
            if fmt not in ("json", "yaml", "toml"):
                self.send_error_json(415, "unsupported_format", f"Unsupported format: {fmt}")
                return
            if len(raw.encode('utf-8')) > 1024 * 1024:
                self.send_error_json(413, "too_large", "Config too large")
                return
            try:
                parsed = parse_raw_config(raw, fmt)
                if not isinstance(parsed, dict):
                    self.send_error_json(422, "unprocessable", "Config must be a JSON object")
                    return
                config = parsed
            except ParseError as e:
                self.send_error_json(422, "unprocessable", str(e),
                                      {"reason": e.reason} if e.reason else None)
                return
        else:
            self.send_error_json(400, "bad_request", "Missing 'config' or 'raw_config' field")
            return

        config = normalize_value(config)
        effective_ref, effective_schema = self.resolve_effective_schema(name, scope, schema_ref)
        if effective_ref is None and schema_ref is not None:
            return

        if effective_schema:
            try:
                validate_config_against_schema(config, effective_schema)
            except SchemaValidationError as e:
                self.send_validation_error(e)
                return

        version = self.configs.create_version(name, scope, config, includes, schema_ref)
        self.send_json(201, {"name": name, "scope": scope, "version": version,
                              "status": "draft", "active": False})

    def list_config_versions(self, name, body):
        scope = body.get("scope", {})
        key = (name, self.configs.scope_to_key(scope))
        versions = sorted(self.configs.configs.get(key, {}).keys())
        self.send_json(200, {"name": name, "scope": scope, "versions": versions})

    def get_config_version(self, name, version, body):
        scope = body.get("scope", {})
        entry = self.configs.get_version(name, scope, version)
        if entry is None:
            self.send_error_json(404, "not_found", "Config version not found")
            return

        active_version = self.configs.get_active_version(name, scope)
        self.send_json(200, {
            "name": name, "scope": scope, "version": version,
            "config": entry["config"], "includes": entry.get("includes", []),
            "status": self.configs.get_version_status(name, scope, version),
            "active": active_version == version
        })

    def resolve_config(self, name, body):
        scope = body.get("scope", {})
        version = body.get("version")
        schema_ref = body.get("schema_ref")

        if self.configs.get_latest_version(name, scope) is None:
            self.send_error_json(404, "not_found", "Config not found")
            return

        try:
            resolved, chain = self.configs.resolve_config(name, scope, version)
        except ValueError as e:
            self.send_error_json(400, "bad_request", str(e))
            return

        effective_ref, effective_schema = self.resolve_effective_schema(name, scope, schema_ref)
        if effective_ref is None and schema_ref is not None:
            return

        response = {
            "name": name, "scope": scope,
            "resolved_config": normalize_value(resolved),
            "inheritance_chain": chain
        }

        if effective_schema:
            try:
                validate_config_against_schema(resolved, effective_schema)
                response["validated_against"] = effective_ref
            except SchemaValidationError as e:
                self.send_validation_error(e)
                return

        self.send_json(200, response)

    def validate_config(self, name, body):
        scope = body.get("scope", {})
        version = body.get("version")
        schema_ref = body.get("schema_ref")
        mode = body.get("mode", "resolved")

        if mode not in ("stored", "resolved"):
            self.send_error_json(400, "bad_request", "Invalid mode")
            return

        latest = self.configs.get_latest_version(name, scope)
        if latest is None:
            self.send_error_json(404, "not_found", "Config not found")
            return

        version_used = version or latest
        entry = self.configs.get_version(name, scope, version_used)
        if entry is None:
            self.send_error_json(404, "not_found", "Config version not found")
            return

        effective_ref, effective_schema = self.resolve_effective_schema(name, scope, schema_ref)
        if effective_ref is None and schema_ref is not None:
            return

        if effective_schema is None:
            self.send_error_json(404, "schema_not_bound", "No schema bound")
            return

        config_to_validate = entry["config"] if mode == "stored" else \
            self.configs.resolve_config(name, scope, version_used)[0]

        try:
            validate_config_against_schema(config_to_validate, effective_schema)
            self.send_json(200, {
                "name": name, "scope": scope, "version_used": version_used,
                "mode": mode, "valid": True, "validated_against": effective_ref
            })
        except SchemaValidationError as e:
            self.send_validation_error(e)

    def bind_schema(self, name, body):
        scope = body.get("scope", {})
        schema_ref = body.get("schema_ref")

        if schema_ref is None:
            self.send_error_json(400, "bad_request", "Missing 'schema_ref'")
            return

        schema_name = schema_ref.get("name")
        schema_version = schema_ref.get("version")

        if not schema_name or schema_version is None:
            self.send_error_json(400, "bad_request", "Invalid 'schema_ref'")
            return

        if not self.schemas.exists(schema_name, schema_version):
            self.send_error_json(404, "not_found", "Schema version not found")
            return

        binding = self.configs.set_binding(name, scope, schema_ref)
        self.send_json(200, binding)

    def get_binding(self, name, body):
        scope = body.get("scope", {})
        binding = self.configs.get_binding(name, scope)
        if binding is None:
            self.send_error_json(404, "not_found", "No binding found")
            return
        self.send_json(200, {
            "name": binding["name"], "scope": binding["scope"],
            "schema_ref": binding["schema_ref"]
        })

    def get_set_policy(self, name, body):
        scope = body.get("scope", {})

        if "required_approvals" not in body and "allow_author_approval" not in body:
            self.send_json(200, self.proposals.get_policy(name, scope))
            return

        required = body.get("required_approvals", 2)
        allow_author = body.get("allow_author_approval", False)
        allowed = body.get("allowed_reviewers")

        if not isinstance(required, int) or required < 1 or required > 10:
            self.send_error_json(422, "policy_violation", "required_approvals must be integer in [1, 10]")
            return

        if allowed is not None:
            if not isinstance(allowed, list):
                self.send_error_json(400, "bad_request", "allowed_reviewers must be a list or null")
                return
            for r in allowed:
                if not isinstance(r, str) or len(r.encode('utf-8')) > 128:
                    self.send_error_json(400, "bad_request", "Reviewer must be non-empty string <= 128 bytes")
                    return

        policy = self.proposals.set_policy(name, scope, required, allow_author, allowed)
        self.send_json(200, policy)

    # --- Proposal routes ---

    def route_proposals(self, parts, body):
        if len(parts) < 3:
            self.send_error_json(404, "not_found", "Endpoint not found")
            return

        try:
            proposal_id = int(parts[2])
        except ValueError:
            self.send_error_json(400, "bad_request", "Invalid proposal ID")
            return

        action = parts[3] if len(parts) > 3 else None

        if action in (None, 'get') or (action and action.endswith(':get')):
            self.get_proposal(proposal_id, body)
        elif action in ('review',) or (action and action.endswith(':review')):
            self.review(proposal_id, body)
        elif action in ('merge',) or (action and action.endswith(':merge')):
            self.merge(proposal_id, body)
        elif action in ('withdraw',) or (action and action.endswith(':withdraw')):
            self.withdraw(proposal_id, body)
        else:
            self.send_error_json(404, "not_found", "Endpoint not found")

    def _format_proposal(self, p):
        result = {
            "proposal_id": p["proposal_id"],
            "name": p["name"],
            "scope": p["scope"],
            "draft_version": p["draft_version"],
            "base_version": p["base_version"],
            "author": p["author"],
            "title": p.get("title"),
            "description": p.get("description"),
            "labels": p.get("labels", []),
            "quorum": p["quorum"],
            "status": p["status"],
            "tally": {
                "approvals": p["tally"]["approvals"],
                "rejections": p["tally"]["rejections"],
                "by_actor": dict(sorted(p["tally"]["by_actor"].items()))
            },
            "diffs": p["diffs"]
        }
        if p.get("policy_summary") is not None:
            result["policy_summary"] = p["policy_summary"]
        return result

    def propose(self, name, version, body):
        scope = body.get("scope", {})
        author = body.get("author")
        title = body.get("title")
        description = body.get("description")
        base_version = body.get("base_version")
        labels = body.get("labels", [])

        if not author:
            self.send_error_json(400, "bad_request", "Missing 'author'")
            return
        if base_version is None:
            self.send_error_json(400, "bad_request", "Missing 'base_version'")
            return
        if not isinstance(author, str) or len(author.encode('utf-8')) > 128:
            self.send_error_json(400, "bad_request", "Invalid author")
            return
        if title is not None and (not isinstance(title, str) or len(title.encode('utf-8')) > 200):
            self.send_error_json(400, "bad_request", "title must be <= 200 bytes")
            return
        if description is not None and (not isinstance(description, str) or len(description.encode('utf-8')) > 8192):
            self.send_error_json(400, "bad_request", "description must be <= 8 KiB")
            return
        if labels:
            if not isinstance(labels, list) or len(labels) > 32:
                self.send_error_json(400, "bad_request", "labels must be <= 32 items")
                return
            for label in labels:
                if not isinstance(label, str) or len(label.encode('utf-8')) > 32 or \
                   not re.match(r'^[a-z0-9._-]+$', label):
                    self.send_error_json(400, "bad_request", "Invalid label format")
                    return

        draft_entry = self.configs.get_version(name, scope, version)
        if draft_entry is None:
            self.send_error_json(409, "conflict", "Draft version not found")
            return
        if self.configs.get_version_status(name, scope, version) != "draft":
            self.send_error_json(409, "conflict", "Version is not a draft")
            return
        if self.configs.get_active_version(name, scope) != base_version:
            self.send_error_json(409, "stale_base", "base_version does not match current active")
            return
        if self.proposals.count_proposals_for_identity(name, scope) >= 1000:
            self.send_error_json(409, "conflict", "Maximum proposals exceeded")
            return

        effective_ref, effective_schema = self.resolve_effective_schema(
            name, scope, draft_entry.get("schema_ref")
        )
        if effective_ref is None and draft_entry.get("schema_ref") is not None:
            return

        if effective_schema:
            try:
                validate_config_against_schema(draft_entry["config"], effective_schema)
            except SchemaValidationError as e:
                self.send_validation_error(e)
                return

        try:
            resolved_config, _ = self.configs.resolve_config(name, scope, version)
        except ValueError as e:
            self.send_error_json(400, "bad_request", str(e))
            return

        if effective_schema:
            try:
                validate_config_against_schema(resolved_config, effective_schema)
            except SchemaValidationError as e:
                self.send_validation_error(e)
                return

        policy_result, timed_out = evaluate_policies_for_target(
            self.configs, self.policy_bundles, self.policy_bindings,
            name, scope, version
        )
        if timed_out:
            self.send_error_json(408, "evaluation_timeout", "Policy evaluation exceeded time budget", policy_result)
            return
        if policy_result["tally"]["errors"] > 0:
            self.send_error_json(422, "policy_violation", "Proposal blocked by policy violations", policy_result)
            return

        policy = self.proposals.get_policy(name, scope)
        base_entry = self.configs.get_version(name, scope, base_version) if base_version else None

        if base_entry:
            base_config = base_entry["config"]
            base_includes = base_entry.get("includes", [])
            try:
                base_resolved, _ = self.configs.resolve_config(name, scope, base_version)
            except ValueError:
                base_resolved = {}
        else:
            base_config, base_includes, base_resolved = {}, [], {}

        diffs = compute_diffs(
            base_config, draft_entry["config"],
            base_resolved, resolved_config,
            base_includes, draft_entry.get("includes", [])
        )

        proposal_id = self.proposals.create_proposal(
            name=name, scope=scope, draft_version=version, base_version=base_version,
            author=author, title=title, description=description, labels=labels,
            quorum=policy, diffs=diffs, policy_summary=policy_result
        )

        # Supersede previous open proposals
        for p in self.proposals.get_open_proposals_for_draft(name, scope, version):
            if p["proposal_id"] != proposal_id:
                self.proposals.update_proposal(p["proposal_id"], {"status": "superseded"})

        self.send_json(201, self._format_proposal(self.proposals.get_proposal(proposal_id)))

    def list_proposals(self, name, body):
        scope = body.get("scope", {})
        status = body.get("status", "any")

        valid_statuses = ["open", "approved", "rejected", "merged", "withdrawn", "superseded", "any"]
        if status not in valid_statuses:
            self.send_error_json(400, "bad_request", f"Invalid status: {status}")
            return

        proposals = self.proposals.list_proposals(name, scope, status if status != "any" else None)
        self.send_json(200, {"proposals": [self._format_proposal(p) for p in proposals]})

    def get_proposal(self, proposal_id, body):
        proposal = self.proposals.get_proposal(proposal_id)
        if proposal is None:
            self.send_error_json(404, "not_found", "Proposal not found")
            return
        self.send_json(200, self._format_proposal(proposal))

    def review(self, proposal_id, body):
        proposal = self.proposals.get_proposal(proposal_id)
        if proposal is None:
            self.send_error_json(404, "not_found", "Proposal not found")
            return

        if proposal["status"] in ("merged", "withdrawn", "superseded"):
            self.send_json(200, self._format_proposal(proposal))
            return

        actor = body.get("actor")
        decision = body.get("decision")
        message = body.get("message")

        if not actor:
            self.send_error_json(400, "bad_request", "Missing 'actor'")
            return
        if decision not in ("approve", "reject"):
            self.send_error_json(400, "bad_request", "decision must be 'approve' or 'reject'")
            return
        if not isinstance(actor, str) or len(actor.encode('utf-8')) > 128:
            self.send_error_json(400, "bad_request", "Invalid actor")
            return

        review_count = self.proposals.count_reviews_for_proposal(proposal_id)
        if review_count >= 1000 and actor not in proposal["tally"]["by_actor"]:
            self.send_error_json(409, "conflict", "Maximum reviews exceeded")
            return

        quorum = proposal["quorum"]
        allowed = quorum.get("allowed_reviewers")
        allow_author = quorum.get("allow_author_approval", False)
        author = proposal["author"]

        if allowed is not None and actor not in allowed:
            self.send_error_json(422, "policy_violation", "Actor not in allowed_reviewers")
            return
        if decision == "approve" and actor == author and not allow_author:
            self.send_error_json(422, "policy_violation", "Author cannot approve own proposal")
            return

        current = proposal["tally"]["by_actor"].get(actor)
        if current and current.get("decision") == decision and current.get("message") == message:
            self.send_json(200, self._format_proposal(proposal))
            return

        self.proposals.add_review(proposal_id, actor, decision, message)
        self.send_json(200, self._format_proposal(self.proposals.get_proposal(proposal_id)))

    def merge(self, proposal_id, body=None):
        body = body or {}
        proposal = self.proposals.get_proposal(proposal_id)
        if proposal is None:
            self.send_error_json(404, "not_found", "Proposal not found")
            return

        if proposal["status"] == "merged":
            self.send_json(200, {
                "activated_version": proposal["draft_version"],
                "previous_active": proposal["base_version"],
                "proposal_id": proposal_id
            })
            return

        if proposal["status"] in ("withdrawn", "superseded"):
            self.send_error_json(409, "conflict", "Proposal is closed")
            return
        if proposal["status"] != "approved":
            self.send_error_json(409, "conflict", "Proposal is not approved")
            return

        name, scope = proposal["name"], proposal["scope"]
        draft_version, base_version = proposal["draft_version"], proposal["base_version"]

        if self.configs.get_active_version(name, scope) != base_version:
            self.send_error_json(409, "stale_base", "Base version has changed since proposal was created")
            return

        draft_entry = self.configs.get_version(name, scope, draft_version)
        if draft_entry is None:
            self.send_error_json(409, "conflict", "Draft version not found")
            return

        effective_ref, effective_schema = self.resolve_effective_schema(name, scope, draft_entry.get("schema_ref"))

        try:
            resolved, _ = self.configs.resolve_config(name, scope, draft_version)
        except ValueError as e:
            self.send_error_json(409, "not_mergeable", str(e))
            return

        if effective_schema:
            try:
                validate_config_against_schema(resolved, effective_schema)
            except SchemaValidationError:
                self.send_error_json(409, "not_mergeable", "Schema validation failed")
                return

        policy_result, timed_out = evaluate_policies_for_target(
            self.configs, self.policy_bundles, self.policy_bindings,
            name, scope, draft_version
        )
        if timed_out:
            self.send_error_json(408, "evaluation_timeout", "Policy evaluation exceeded time budget", policy_result)
            return
        if policy_result["tally"]["errors"] > 0:
            self.send_error_json(409, "not_mergeable", "Merge blocked by policy violations", policy_result)
            return

        self.configs.set_active_version(name, scope, draft_version)
        self.proposals.update_proposal(proposal_id, {"status": "merged"})

        for p in self.proposals.get_open_proposals_for_identity(name, scope):
            if p["proposal_id"] != proposal_id:
                self.proposals.update_proposal(p["proposal_id"], {"status": "superseded"})

        self.send_json(200, {
            "activated_version": draft_version,
            "previous_active": base_version,
            "proposal_id": proposal_id
        })

    def withdraw(self, proposal_id, body):
        proposal = self.proposals.get_proposal(proposal_id)
        if proposal is None:
            self.send_error_json(404, "not_found", "Proposal not found")
            return

        if proposal["status"] in ("merged", "withdrawn", "superseded"):
            self.send_json(200, self._format_proposal(proposal))
            return

        actor = body.get("actor")
        if not actor:
            self.send_error_json(400, "bad_request", "Missing 'actor'")
            return
        if actor != proposal["author"]:
            self.send_error_json(422, "policy_violation", "Only author can withdraw")
            return

        self.proposals.update_proposal(proposal_id, {"status": "withdrawn"})
        self.send_json(200, self._format_proposal(self.proposals.get_proposal(proposal_id)))

    def activate(self, name, version, body):
        scope = body.get("scope", {})
        proposal_id = body.get("proposal_id")

        if proposal_id is None:
            self.send_error_json(409, "approval_required", "Proposal required for activation")
            return

        proposal = self.proposals.get_proposal(proposal_id)
        if proposal is None:
            self.send_error_json(409, "approval_required", "Proposal not found")
            return

        if proposal["name"] != name or proposal["draft_version"] != version:
            self.send_error_json(409, "approval_required", "Proposal does not match target")
            return
        if self.configs.scope_to_key(scope) != self.configs.scope_to_key(proposal["scope"]):
            self.send_error_json(409, "approval_required", "Proposal scope does not match")
            return
        if proposal["status"] != "approved":
            self.send_error_json(409, "approval_required", "Proposal is not approved")
            return
        if self.configs.get_active_version(name, scope) != proposal["base_version"]:
            self.send_error_json(409, "stale_base", "Base version has changed")
            return

        self.merge(proposal_id, body)

    def rollback(self, name, body):
        scope = body.get("scope", {})
        proposal_id = body.get("proposal_id")

        if proposal_id is None:
            self.send_error_json(409, "approval_required", "Proposal required for rollback")
            return

        proposal = self.proposals.get_proposal(proposal_id)
        if proposal is None:
            self.send_error_json(409, "approval_required", "Proposal not found")
            return

        if proposal["name"] != name:
            self.send_error_json(409, "approval_required", "Proposal does not match target")
            return
        if self.configs.scope_to_key(scope) != self.configs.scope_to_key(proposal["scope"]):
            self.send_error_json(409, "approval_required", "Proposal scope does not match")
            return
        if proposal["status"] not in ("approved", "merged"):
            self.send_error_json(409, "approval_required", "Proposal is not approved")
            return
        if self.configs.get_active_version(name, scope) != proposal["base_version"]:
            self.send_error_json(409, "stale_base", "Base version has changed")
            return

        if proposal["status"] == "merged":
            self.send_json(200, {
                "activated_version": proposal["draft_version"],
                "previous_active": proposal["base_version"],
                "proposal_id": proposal_id
            })
            return

        self.merge(proposal_id, body)

    # --- Policy routes ---

    def route_policies(self, parts, body):
        if len(parts) < 3:
            self.send_error_json(404, "not_found", "Endpoint not found")
            return

        sub = parts[2]
        if sub == 'bundles':
            self.route_policy_bundles(parts, body)
        elif sub == 'bindings':
            self.route_policy_bindings(parts, body)
        elif sub == 'stack':
            self.policy_stack(body)
        elif sub == 'evaluate':
            self.policy_evaluate(body)
        elif sub == 'explain':
            self.policy_explain(body)
        else:
            self.send_error_json(404, "not_found", "Endpoint not found")

    def route_policy_bundles(self, parts, body):
        if len(parts) < 4:
            self.send_error_json(404, "not_found", "Endpoint not found")
            return

        bundle_name = parts[3]

        if len(parts) == 5 and parts[4] == 'versions':
            self.create_policy_bundle(bundle_name, body)
            return
        if len(parts) == 6 and parts[4] == 'versions':
            action = parts[5]
            if action == 'list':
                self.list_policy_bundle_versions(bundle_name, body)
                return
            if action.endswith(':get'):
                try:
                    self.get_policy_bundle_version(bundle_name, int(action[:-4]), body)
                except ValueError:
                    self.send_error_json(400, "bad_request", "Invalid version number")
                return
        if len(parts) == 7 and parts[4] == 'versions' and parts[6] == 'get':
            try:
                self.get_policy_bundle_version(bundle_name, int(parts[5]), body)
            except ValueError:
                self.send_error_json(400, "bad_request", "Invalid version number")
            return

        self.send_error_json(404, "not_found", "Endpoint not found")

    def route_policy_bindings(self, parts, body):
        if len(parts) == 3:
            self.create_policy_binding(body)
        else:
            self.send_error_json(404, "not_found", "Endpoint not found")

    def _extract_tarball(self, tarball_data, default_data):
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
                if name.startswith('./'):
                    name = name[2:]

                if name.endswith('.rego'):
                    rego_modules[os.path.basename(name)] = content
                elif name.endswith('.json') or name.endswith('.yaml') or name.endswith('.yml'):
                    try:
                        parsed = json.loads(content) if name.endswith('.json') else yaml.safe_load(content)
                        if isinstance(parsed, dict):
                            data.update(parsed)
                    except Exception:
                        pass

        return rego_modules, data

    def create_policy_bundle(self, bundle_name, body):
        data = body.get("data", {})
        metadata = body.get("metadata", {})

        if "rego_modules" in body:
            rego_modules = body["rego_modules"]
            if not isinstance(rego_modules, dict):
                self.send_error_json(422, "policy_invalid", "rego_modules must be an object")
                return
        elif "tarball_b64" in body:
            try:
                tarball_data = base64.b64decode(body["tarball_b64"])
                rego_modules, data = self._extract_tarball(tarball_data, data)
            except Exception as e:
                self.send_error_json(422, "policy_invalid", f"Failed to decode tarball: {e}")
                return
        else:
            self.send_error_json(422, "policy_invalid", "Missing rego_modules or tarball_b64")
            return

        if not rego_modules:
            self.send_error_json(422, "policy_invalid", "No rego modules provided")
            return

        has_deny_or_warn = False
        for mod_name, mod_code in rego_modules.items():
            if not isinstance(mod_code, str):
                self.send_error_json(422, "policy_invalid", f"Module {mod_name} must be a string")
                return
            try:
                rules = RegoParser.parse(mod_code)
                if any(r.get("kind") in ("deny", "warn") for r in rules):
                    has_deny_or_warn = True
            except Exception as e:
                self.send_error_json(422, "policy_invalid", f"Failed to parse module {mod_name}: {e}")
                return

        if not has_deny_or_warn:
            self.send_error_json(422, "policy_invalid", "Bundle must contain at least one deny or warn rule")
            return

        for mod_code in rego_modules.values():
            if re.search(r'http\.(get|post|put|delete|patch)', mod_code):
                self.send_error_json(422, "policy_invalid", "Network I/O not allowed in policies")
                return
            if re.search(r'opa\.runtime', mod_code):
                self.send_error_json(422, "policy_invalid", "Runtime access not allowed in policies")
                return

        test_input = {
            "target": {"name": "", "scope": {}, "version_used": 0,
                       "resolved_config": {}, "provenance": []},
            "graph": {"by_name": {}},
            "now": "2025-01-01T00:00:00Z"
        }
        try:
            result = RegoEngine.evaluate(rego_modules, test_input, data)
            if not isinstance(result.get("deny"), list) or not isinstance(result.get("warn"), list):
                self.send_error_json(422, "policy_invalid", "data.guardrails.deny/warn must evaluate to arrays")
                return
        except Exception as e:
            self.send_error_json(422, "policy_invalid", f"Bundle evaluation test failed: {e}")
            return

        try:
            version = self.policy_bundles.create_version(bundle_name, rego_modules, data, metadata)
        except ValueError as e:
            if "1 MiB" in str(e) or "size" in str(e).lower():
                self.send_error_json(413, "too_large", str(e))
            else:
                self.send_error_json(409, "policy_conflict", str(e))
            return

        self.send_json(201, {"bundle_name": bundle_name, "version": version})

    def list_policy_bundle_versions(self, bundle_name, body):
        versions = self.policy_bundles.get_versions_with_metadata(bundle_name)
        self.send_json(200, {"bundle_name": bundle_name, "versions": versions})

    def get_policy_bundle_version(self, bundle_name, version, body):
        entry = self.policy_bundles.get_version(bundle_name, version)
        if entry is None:
            self.send_error_json(404, "policy_not_found", "Bundle version not found")
            return
        self.send_json(200, {
            "bundle_name": bundle_name, "version": version,
            "rego_modules": entry["rego_modules"],
            "data": entry.get("data", {}),
            "metadata": entry.get("metadata", {})
        })

    def create_policy_binding(self, body):
        bundle_ref = body.get("bundle", {})
        if bundle_ref:
            bundle_name = bundle_ref.get("name")
            bundle_version = bundle_ref.get("version")
        else:
            bundle_name = body.get("policy_bundle")
            bundle_version = body.get("policy_version")

        selector = body.get("selector") or body.get("selectors")
        graph_keys = body.get("graph_keys", ["env", "tenant"])
        priority = body.get("priority", 0)

        if not bundle_name or bundle_version is None:
            self.send_error_json(400, "invalid_input", "Missing bundle name/version")
            return
        if not self.policy_bundles.exists(bundle_name, bundle_version):
            self.send_error_json(404, "policy_not_found", "Bundle version not found")
            return
        if not selector or not isinstance(selector, dict):
            self.send_error_json(400, "invalid_input", "Selector must be a non-empty object")
            return
        if not all(isinstance(k, str) and isinstance(v, (str, int, float, bool))
                   for k, v in selector.items()):
            self.send_error_json(400, "invalid_input", "Selector values must be primitives")
            return
        if not isinstance(graph_keys, list):
            self.send_error_json(400, "invalid_input", "graph_keys must be an array")
            return

        if self.policy_bindings.check_duplicate(bundle_name, bundle_version, selector, priority):
            self.send_error_json(409, "policy_conflict", "Duplicate binding")
            return

        try:
            _, binding = self.policy_bindings.create_binding(
                bundle_name, bundle_version, selector, graph_keys, priority
            )
        except ValueError as e:
            self.send_error_json(409, "policy_conflict", str(e))
            return

        self.send_json(201, binding)

    def policy_stack(self, body):
        name = body.get("name")
        scope = body.get("scope", {})

        if not name:
            self.send_error_json(400, "invalid_input", "Missing name")
            return

        matching = self.policy_bindings.get_matching_bindings(name, scope)
        stack = [{
            "bundle": {"name": b["bundle"]["name"], "version": b["bundle"]["version"]},
            "selector": b["selector"],
            "graph_keys": b["graph_keys"],
            "priority": b["priority"]
        } for b in matching]

        self.send_json(200, stack)

    def policy_evaluate(self, body):
        name = body.get("name")
        scope = body.get("scope", {})
        version = body.get("version")
        include_graph = body.get("include_graph", True)

        if not name:
            self.send_error_json(400, "invalid_input", "Missing name")
            return

        if version is None:
            version = self.configs.get_active_version(name, scope) or \
                      self.configs.get_latest_version(name, scope)

        if version is None:
            self.send_error_json(404, "not_found", "Config not found")
            return

        entry = self.configs.get_version(name, scope, version)
        if entry is None:
            self.send_error_json(404, "not_found", "Config version not found")
            return

        result, timed_out = evaluate_policies_for_target(
            self.configs, self.policy_bundles, self.policy_bindings,
            name, scope, version, include_graph
        )

        if timed_out:
            self.send_error_json(408, "evaluation_timeout", "Policy evaluation exceeded time budget",
                                  result if result.get("violations") else None)
            return

        self.send_json(200, result)

    def policy_explain(self, body):
        violation_input = body.get("violation")
        if not violation_input:
            self.send_error_json(400, "invalid_input", "Missing violation")
            return

        policy_ref = violation_input.get("policy", {})
        target_ref = violation_input.get("target", {})
        rule_id = violation_input.get("rule_id", "")
        path = violation_input.get("path", "")

        policy_name = policy_ref.get("name", "")
        policy_version = policy_ref.get("version")
        target_name = target_ref.get("name", "")
        target_scope = target_ref.get("scope", {})

        lines = []

        matching = self.policy_bindings.get_matching_bindings(target_name, target_scope)
        for b in matching:
            if b["bundle"]["name"] == policy_name and b["bundle"]["version"] == policy_version:
                selector_parts = [f"{k}={v}" for k, v in sorted(b["selector"].items())]
                lines.append(f"Selector matched: {' '.join(selector_parts)}")
                break

        try:
            version = target_ref.get("version_used") or \
                     self.configs.get_active_version(target_name, target_scope) or \
                     self.configs.get_latest_version(target_name, target_scope)

            if version is not None:
                resolved, _ = self.configs.resolve_config(target_name, target_scope, version)
                scope_parts = [str(v) for v in sorted(target_scope.values())]
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

        if not lines:
            lines.append(f"Rule {rule_id} triggered for {target_name} at {path}")
        lines.append("Decision: DENY (error)")

        self.send_json(200, {"explain": lines})


# --- Server startup ---

def run_server(address="0.0.0.0", port=8080):
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
