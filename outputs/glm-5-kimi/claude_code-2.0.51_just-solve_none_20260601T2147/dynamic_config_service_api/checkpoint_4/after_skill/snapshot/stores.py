"""Storage classes for schemas, configs, proposals, and policies."""

import json
import threading
from copy import deepcopy
from parsers import normalize_value
from json_utils import deep_merge


class SchemaRegistry:
    MAX_VERSIONS = 1000

    def __init__(self):
        self.schemas = {}

    def create(self, name, schema):
        if name not in self.schemas:
            self.schemas[name] = {}
        versions = self.schemas[name]
        if len(versions) >= self.MAX_VERSIONS:
            raise ValueError("Maximum schema versions exceeded")
        new_version = max(versions.keys(), default=0) + 1
        versions[new_version] = deepcopy(schema)
        return new_version

    def get(self, name, version):
        return self.schemas.get(name, {}).get(version)

    def list_versions(self, name):
        return sorted(self.schemas.get(name, {}).keys())

    def exists(self, name, version):
        return name in self.schemas and version in self.schemas[name]


class ConfigStore:
    def __init__(self):
        self.configs = {}
        self.bindings = {}
        self.active_versions = {}
        self.version_status = {}

    @staticmethod
    def scope_to_key(scope):
        return json.dumps(sorted(scope.items()))

    @staticmethod
    def key_to_scope(key):
        return dict(json.loads(key))

    def _key(self, name, scope):
        return (name, self.scope_to_key(scope))

    def create_version(self, name, scope, config, includes=None, schema_ref=None):
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

    def get_version(self, name, scope, version):
        return self.configs.get(self._key(name, scope), {}).get(version)

    def get_latest_version(self, name, scope):
        versions = self.configs.get(self._key(name, scope))
        return max(versions.keys()) if versions else None

    def get_active_version(self, name, scope):
        return self.active_versions.get(self._key(name, scope))

    def set_active_version(self, name, scope, version):
        key = self._key(name, scope)
        self.active_versions[key] = version
        self.version_status[(name, key[1], version)] = "active"

    def get_version_status(self, name, scope, version):
        return self.version_status.get((name, self._key(name, scope)[1], version), "draft")

    def get_config_entry(self, name, scope, version=None):
        if version is None:
            version = self.get_active_version(name, scope) or self.get_latest_version(name, scope)
        return self.get_version(name, scope, version) if version else None

    def set_binding(self, name, scope, schema_ref):
        binding = {"name": name, "scope": deepcopy(scope), "schema_ref": deepcopy(schema_ref), "active": True}
        self.bindings[self._key(name, scope)] = binding
        return binding

    def get_binding(self, name, scope):
        return self.bindings.get(self._key(name, scope))

    def get_all_configs(self):
        results = []
        for (name, scope_key), versions in self.configs.items():
            scope = self.key_to_scope(scope_key)
            for version, entry in versions.items():
                results.append((name, scope, version, entry))
        return results

    def resolve_config(self, name, scope, version=None, visited=None):
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
            inc_scope = deepcopy(scope)
            inc_scope.update(inc.get("scope", {}))
            inc_version = inc.get("version")

            inc_config, inc_chain = self.resolve_config(inc_name, inc_scope, inc_version, visited.copy())
            inheritance_chain.extend(inc_chain)
            merged = deep_merge(merged, inc_config)

        merged = deep_merge(merged, config)
        return merged, inheritance_chain


class ProposalStore:
    def __init__(self):
        self.proposals = {}
        self.next_proposal_id = 1
        self.policies = {}

    def create_proposal(self, name, scope, draft_version, base_version, author,
                        title=None, description=None, labels=None, quorum=None,
                        diffs=None, policy_summary=None):
        proposal_id = self.next_proposal_id
        self.next_proposal_id += 1

        self.proposals[proposal_id] = {
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
            "tally": {"approvals": 0, "rejections": 0, "by_actor": {}},
            "diffs": diffs or {"raw_json_patch": [], "resolved_json_patch": [],
                               "includes_changes": [], "human": []},
            "policy_summary": policy_summary
        }
        return proposal_id

    def get_proposal(self, proposal_id):
        return self.proposals.get(proposal_id)

    def update_proposal(self, proposal_id, updates):
        if proposal_id in self.proposals:
            self.proposals[proposal_id].update(updates)

    def _find_proposals(self, name, scope, status=None, draft_version=None):
        scope_key = ConfigStore.scope_to_key(scope)
        return [
            p for p in self.proposals.values()
            if p["name"] == name and
               ConfigStore.scope_to_key(p["scope"]) == scope_key and
               (draft_version is None or p["draft_version"] == draft_version) and
               (status is None or p["status"] == status)
        ]

    def list_proposals(self, name, scope, status=None):
        return sorted(self._find_proposals(name, scope, status), key=lambda p: p["proposal_id"])

    def get_open_proposals_for_draft(self, name, scope, draft_version):
        return self._find_proposals(name, scope, status="open", draft_version=draft_version)

    def get_open_proposals_for_identity(self, name, scope):
        return self._find_proposals(name, scope, status="open")

    def add_review(self, proposal_id, actor, decision, message=None):
        proposal = self.proposals.get(proposal_id)
        if not proposal:
            return

        tally = proposal["tally"]
        by_actor = tally["by_actor"]

        if actor in by_actor:
            prev = by_actor[actor].get("decision")
            if prev == "approve":
                tally["approvals"] -= 1
            elif prev == "reject":
                tally["rejections"] -= 1

        by_actor[actor] = {"decision": decision, "message": message}
        if decision == "approve":
            tally["approvals"] += 1
        elif decision == "reject":
            tally["rejections"] += 1

        proposal["status"] = self._calculate_status(proposal)

    def _calculate_status(self, proposal):
        if proposal["status"] in ("merged", "withdrawn", "superseded"):
            return proposal["status"]
        if proposal["tally"]["rejections"] > 0:
            return "rejected"

        quorum = proposal["quorum"]
        required = quorum.get("required_approvals", 2)
        allow_author = quorum.get("allow_author_approval", False)

        approvers = {
            actor for actor, review in proposal["tally"]["by_actor"].items()
            if review.get("decision") == "approve" and (allow_author or actor != proposal.get("author"))
        }
        return "approved" if len(approvers) >= required else "open"

    def set_policy(self, name, scope, required_approvals, allow_author_approval, allowed_reviewers=None):
        key = (name, ConfigStore.scope_to_key(scope))
        self.policies[key] = {
            "required_approvals": required_approvals,
            "allow_author_approval": allow_author_approval,
            "allowed_reviewers": sorted(allowed_reviewers) if allowed_reviewers else None
        }
        return self.policies[key]

    def get_policy(self, name, scope):
        key = (name, ConfigStore.scope_to_key(scope))
        if key in self.policies:
            return deepcopy(self.policies[key])
        return {"required_approvals": 2, "allow_author_approval": False, "allowed_reviewers": None}

    def count_proposals_for_identity(self, name, scope):
        return len(self._find_proposals(name, scope))

    def count_reviews_for_proposal(self, proposal_id):
        proposal = self.proposals.get(proposal_id)
        return len(proposal["tally"]["by_actor"]) if proposal else 0


class PolicyBundleStore:
    MAX_BUNDLES = 500
    MAX_VERSIONS_PER_BUNDLE = 200
    MAX_REGO_SIZE = 1024 * 1024

    def __init__(self):
        self.bundles = {}
        self._lock = threading.Lock()

    def create_version(self, bundle_name, rego_modules, data=None, metadata=None):
        with self._lock:
            if bundle_name not in self.bundles:
                if len(self.bundles) >= self.MAX_BUNDLES:
                    raise ValueError("Maximum number of bundles exceeded")
                self.bundles[bundle_name] = {}

            versions = self.bundles[bundle_name]
            if len(versions) >= self.MAX_VERSIONS_PER_BUNDLE:
                raise ValueError("Maximum versions per bundle exceeded")

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

    def get_version(self, bundle_name, version):
        return self.bundles.get(bundle_name, {}).get(version)

    def list_versions(self, bundle_name):
        return sorted(self.bundles.get(bundle_name, {}).keys())

    def exists(self, bundle_name, version):
        return bundle_name in self.bundles and version in self.bundles[bundle_name]

    def get_versions_with_metadata(self, bundle_name):
        if bundle_name not in self.bundles:
            return []
        return [
            {"version": v, "metadata": self.bundles[bundle_name][v].get("metadata", {})}
            for v in sorted(self.bundles[bundle_name].keys())
        ]


class PolicyBindingStore:
    MAX_BINDINGS = 5000

    def __init__(self):
        self.bindings = {}
        self._next_id = 1
        self._lock = threading.Lock()

    def create_binding(self, bundle_name, bundle_version, selector, graph_keys=None, priority=0):
        with self._lock:
            if len(self.bindings) >= self.MAX_BINDINGS:
                raise ValueError("Maximum bindings exceeded")

            binding_id = str(self._next_id)
            self._next_id += 1

            binding = {
                "binding_id": binding_id,
                "bundle": {"name": bundle_name, "version": bundle_version},
                "selector": normalize_value(deepcopy(selector)),
                "graph_keys": sorted(graph_keys) if graph_keys else ["env", "tenant"],
                "priority": priority
            }
            self.bindings[binding_id] = binding
            return binding_id, deepcopy(binding)

    def check_duplicate(self, bundle_name, bundle_version, selector, priority):
        sel_normalized = normalize_value(selector)
        return any(
            b["bundle"]["name"] == bundle_name and
            b["bundle"]["version"] == bundle_version and
            b["selector"] == sel_normalized and
            b["priority"] == priority
            for b in self.bindings.values()
        )

    def get_matching_bindings(self, name, scope):
        matches = [
            b for b in self.bindings.values()
            if all(scope.get(k) == v for k, v in b["selector"].items())
        ]
        matches.sort(key=lambda b: (-b["priority"], b["bundle"]["name"], b["bundle"]["version"]))
        return matches

    def get_binding(self, binding_id):
        return self.bindings.get(binding_id)
