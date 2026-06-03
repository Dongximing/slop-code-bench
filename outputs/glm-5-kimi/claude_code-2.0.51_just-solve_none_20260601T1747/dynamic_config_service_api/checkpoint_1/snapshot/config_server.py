#!/usr/bin/env python3
"""Configuration server with immutable versions, scoping, rollback, and imports."""

import argparse
import hashlib
import json
import sys
import threading
from collections import OrderedDict
from copy import deepcopy

from flask import Flask, Response, jsonify, request

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

class ConfigStore:
    """Thread-safe in-memory store for config versions."""

    MAX_VERSIONS = 10_000
    MAX_INCLUDE_CHAIN = 64
    MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MiB

    def __init__(self):
        self._lock = threading.Lock()
        # key = (name, scope_key) -> OrderedDict of version -> ConfigVersion
        self._configs: dict[tuple[str, str], OrderedDict[int, "ConfigVersion"]] = {}
        # key = (name, scope_key) -> active version number
        self._active: dict[tuple[str, str], int] = {}
        # idempotency: hash of last create body -> (name, scope_key, version)
        self._idempotency: dict[str, tuple] = {}

    @staticmethod
    def scope_key(scope: dict[str, str]) -> str:
        """Deterministic string key for a scope dict."""
        return json.dumps(scope, sort_keys=True, separators=(",", ":"))

    def _pair_key(self, name: str, scope: dict[str, str]) -> tuple[str, str]:
        return (name, self.scope_key(scope))

    def create(
        self,
        name: str,
        scope: dict[str, str],
        config: dict,
        includes: list | None,
        inherits_active: bool,
        body_hash: str,
    ) -> tuple[dict | None, dict | None, int | None]:
        """
        Create a new version. Returns (result_dict, error_dict, http_status).
        On idempotent duplicate, returns the existing version.
        """
        with self._lock:
            pair = self._pair_key(name, scope)

            # Idempotency check
            existing = self._idempotency.get(body_hash)
            if existing is not None:
                ename, escope_key, eversion = existing
                if ename == name and escope_key == pair[1]:
                    ev = self._configs[pair][eversion]
                    return {
                        "name": name,
                        "scope": scope,
                        "version": eversion,
                        "active": self._active.get(pair) == eversion,
                    }, None, None  # signal idempotent hit

            versions = self._configs.get(pair)
            next_version = 1
            if versions is None:
                versions = OrderedDict()
                self._configs[pair] = versions
            else:
                if len(versions) >= self.MAX_VERSIONS:
                    return None, self._error("conflict", "Max versions (10,000) reached for this config"), 409
                next_version = max(versions.keys()) + 1

            # Handle inherits_active
            if inherits_active and pair in self._active:
                active_ver = self._active[pair]
                active_cv = versions[active_ver]
                # Merge: start with active config, overlay new config
                merged_config = self._deep_merge_simple(active_cv.config, config)
                config = merged_config
                # Merge includes: start with active includes + new includes
                if includes is None:
                    includes = deepcopy(active_cv.includes)
                else:
                    if active_cv.includes:
                        includes = deepcopy(active_cv.includes) + includes

            if includes is None:
                includes = []

            cv = ConfigVersion(
                name=name,
                scope=scope,
                version=next_version,
                config=config,
                includes=includes,
            )
            versions[next_version] = cv
            self._active[pair] = next_version
            self._idempotency[body_hash] = (name, pair[1], next_version)

            return {
                "name": name,
                "scope": scope,
                "version": next_version,
                "active": True,
            }, None, 201

    def list_versions(self, name: str, scope: dict[str, str]) -> tuple[dict, int]:
        pair = self._pair_key(name, scope)
        with self._lock:
            versions = self._configs.get(pair)
            if versions is None:
                return {
                    "name": name,
                    "scope": scope,
                    "versions": [],
                }, 200
            active_ver = self._active.get(pair)
            result = []
            for v in sorted(versions.keys()):
                result.append({
                    "version": v,
                    "active": v == active_ver,
                })
            return {
                "name": name,
                "scope": scope,
                "versions": result,
            }, 200

    def get_version(self, name: str, scope: dict[str, str], version: int) -> tuple[dict | None, int]:
        pair = self._pair_key(name, scope)
        with self._lock:
            versions = self._configs.get(pair)
            if versions is None or version not in versions:
                return None, 404
            cv = versions[version]
            active_ver = self._active.get(pair)
            return {
                "name": name,
                "scope": scope,
                "version": version,
                "active": version == active_ver,
                "config": cv.config,
                "includes": cv.includes,
            }, 200

    def get_active(self, name: str, scope: dict[str, str]) -> tuple[dict | None, int]:
        pair = self._pair_key(name, scope)
        with self._lock:
            active_ver = self._active.get(pair)
            if active_ver is None:
                return None, 404
            versions = self._configs.get(pair)
            cv = versions[active_ver]
            return {
                "name": name,
                "scope": scope,
                "version": active_ver,
                "active": True,
                "config": cv.config,
                "includes": cv.includes,
            }, 200

    def activate(self, name: str, scope: dict[str, str], version: int) -> tuple[dict | None, int]:
        pair = self._pair_key(name, scope)
        with self._lock:
            versions = self._configs.get(pair)
            if versions is None or version not in versions:
                return None, 404
            self._active[pair] = version
            return {
                "name": name,
                "scope": scope,
                "version": version,
                "active": True,
            }, 200

    def rollback(self, name: str, scope: dict[str, str], to_version: int) -> tuple[dict | None, int]:
        pair = self._pair_key(name, scope)
        with self._lock:
            active_ver = self._active.get(pair)
            if active_ver is not None and to_version > active_ver:
                return None, 409  # conflict: can't roll forward
            versions = self._configs.get(pair)
            if versions is None or to_version not in versions:
                return None, 404
            if active_ver == to_version:
                # Already active, idempotent
                return {
                    "name": name,
                    "scope": scope,
                    "version": to_version,
                    "active": True,
                }, 200
            self._active[pair] = to_version
            return {
                "name": name,
                "scope": scope,
                "version": to_version,
                "active": True,
            }, 200

    def resolve(
        self,
        name: str,
        scope: dict[str, str],
        version: int | None,
        dry_run: bool,
    ) -> tuple[dict | None, dict | None, int]:
        """Resolve config with imports. Returns (result, error, status)."""
        # Do resolution outside the main lock to avoid deadlock on recursive includes.
        # We take the lock only for reads.
        graph: list[dict] = []
        visited: set[tuple[str, str, int]] = set()

        try:
            resolved = self._resolve_recursive(
                name, scope, version, graph, visited, dry_run
            )
        except CycleError as e:
            return None, self._error("cycle_detected", str(e)), 409
        except MergeConflictError as e:
            return None, self._error("unprocessable", str(e), {"path": e.path}), 422
        except MaxDepthError as e:
            return None, self._error("unprocessable", str(e), {"reason": "max_depth"}), 422
        except NotFoundError as e:
            return None, self._error("not_found", str(e)), 404

        return {
            "name": name,
            "scope": scope,
            "version_used": self._get_version_used(name, scope, version),
            "resolved_config": resolved,
            "resolution_graph": graph,
        }, None, 200

    def _get_version_used(self, name: str, scope: dict[str, str], version: int | None) -> int:
        pair = self._pair_key(name, scope)
        with self._lock:
            if version is not None:
                return version
            return self._active.get(pair, 0)

    def _resolve_recursive(
        self,
        name: str,
        scope: dict[str, str],
        version: int | None,
        graph: list[dict],
        visited: set[tuple[str, str, int]],
        dry_run: bool,
    ) -> dict:
        """Recursively resolve a config and its includes."""
        pair = self._pair_key(name, scope)

        with self._lock:
            versions = self._configs.get(pair)
            if versions is None:
                if dry_run:
                    return {}
                raise NotFoundError(f"Config '{name}' with scope not found")
            if version is None:
                active_ver = self._active.get(pair)
                if active_ver is None:
                    if dry_run:
                        return {}
                    raise NotFoundError(f"No active version for '{name}' with scope")
                version = active_ver
            if version not in versions:
                raise NotFoundError(f"Version {version} not found for '{name}' with scope")
            cv = versions[version]
            includes = deepcopy(cv.includes)
            own_config = deepcopy(cv.config)

        # Cycle detection
        trip = (name, self.scope_key(scope), version)
        if trip in visited:
            raise CycleError(f"Cycle detected involving '{name}' version {version}")
        if len(visited) >= self.MAX_INCLUDE_CHAIN:
            raise MaxDepthError("Max include chain length (64) exceeded")
        visited.add(trip)

        try:
            # Start with empty object
            accumulator: dict = {}

            # Process includes in order
            for inc in includes:
                inc_name = inc["name"]
                inc_scope = inc["scope"]
                inc_version = inc.get("version")
                inc_resolved = self._resolve_recursive(
                    inc_name, inc_scope, inc_version, graph, visited, dry_run
                )
                accumulator = self._deep_merge(accumulator, inc_resolved)

            # Merge own config on top
            result = self._deep_merge(accumulator, own_config)

            # Add to graph
            graph.append({
                "name": name,
                "scope": scope,
                "version_used": version,
            })

            return result
        finally:
            visited.discard(trip)

    @staticmethod
    def _deep_merge(base: dict, override: dict, path: str = "") -> dict:
        """Deep merge two dicts. Returns new dict. Raises MergeConflictError on type conflicts."""
        result = deepcopy(base)
        for key in override:
            new_path = f"{path}/{key}" if path else f"/{key}"
            if key in result:
                base_val = result[key]
                over_val = override[key]
                if isinstance(base_val, dict) and isinstance(over_val, dict):
                    result[key] = ConfigStore._deep_merge(base_val, over_val, new_path)
                elif isinstance(base_val, dict) or isinstance(over_val, dict):
                    raise MergeConflictError(
                        f"Type conflict at {new_path}: cannot merge dict with non-dict",
                        new_path,
                    )
                else:
                    # Arrays and scalars: right wins
                    result[key] = deepcopy(over_val)
            else:
                result[key] = deepcopy(override[key])
        return result

    @staticmethod
    def _deep_merge_simple(base: dict, override: dict) -> dict:
        """Simple deep merge for inherits_active - no conflict checking needed."""
        result = deepcopy(base)
        for key in override:
            if key in result and isinstance(result[key], dict) and isinstance(override[key], dict):
                result[key] = ConfigStore._deep_merge_simple(result[key], override[key])
            else:
                result[key] = deepcopy(override[key])
        return result

    @staticmethod
    def _error(code: str, message: str, details: dict | None = None) -> dict:
        return {
            "error": {
                "code": code,
                "message": message,
                "details": details or {},
            }
        }


class ConfigVersion:
    """Immutable config version."""

    __slots__ = ("name", "scope", "version", "config", "includes")

    def __init__(
        self,
        name: str,
        scope: dict[str, str],
        version: int,
        config: dict,
        includes: list,
    ):
        self.name = name
        self.scope = scope
        self.version = version
        self.config = config
        self.includes = includes


class CycleError(Exception):
    pass


class MergeConflictError(Exception):
    def __init__(self, message: str, path: str):
        super().__init__(message)
        self.path = path


class MaxDepthError(Exception):
    pass


class NotFoundError(Exception):
    pass


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

store = ConfigStore()
app = Flask(__name__)

# Disable Flask's default error handling to return JSON
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024  # 1 MiB


def _json_response(data: dict, status: int = 200) -> Response:
    """Create a canonical JSON response."""
    body = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    resp = Response(body + "\n", status=status, content_type="application/json; charset=utf-8")
    return resp


def _error_response(code: str, message: str, status: int, details: dict | None = None) -> Response:
    return _json_response(
        {"error": {"code": code, "message": message, "details": details or {}}},
        status,
    )


def _parse_body():
    """Parse request body as JSON with validation."""
    if request.content_length and request.content_length > ConfigStore.MAX_BODY_BYTES:
        return None, _error_response("too_large", "Request body exceeds 1 MiB limit", 413)

    try:
        data = request.get_json(force=True, silent=True)
    except Exception:
        return None, _error_response("invalid_input", "Invalid JSON in request body", 400)

    if data is None:
        return None, _error_response("invalid_input", "Invalid JSON in request body", 400)

    return data, None


def _validate_scope(data: dict, field: str = "scope") -> tuple[dict | None, Response | None]:
    """Validate scope field from request body."""
    if field not in data:
        return None, _error_response("invalid_input", f"Missing required field: {field}", 400)

    scope = data[field]
    if not isinstance(scope, dict):
        return None, _error_response("invalid_input", f"'{field}' must be a JSON object", 400)

    for k, v in scope.items():
        if not isinstance(k, str) or not isinstance(v, str):
            return None, _error_response(
                "invalid_input", f"'{field}' must be a string-to-string map", 400
            )

    return scope, None


def _validate_includes(includes: list) -> tuple[bool, Response | None]:
    """Validate includes list."""
    for i, inc in enumerate(includes):
        if not isinstance(inc, dict):
            return False, _error_response(
                "invalid_input", f"Include at index {i} must be an object", 400
            )
        if "name" not in inc or not isinstance(inc["name"], str) or not inc["name"]:
            return False, _error_response(
                "invalid_input", f"Include at index {i} must have a non-empty 'name' string", 400
            )
        if "scope" not in inc or not isinstance(inc["scope"], dict):
            return False, _error_response(
                "invalid_input", f"Include at index {i} must have a 'scope' object", 400
            )
        for k, v in inc["scope"].items():
            if not isinstance(k, str) or not isinstance(v, str):
                return False, _error_response(
                    "invalid_input", f"Include at index {i} scope must be string-to-string map", 400
                )
        if "version" in inc and inc["version"] is not None:
            if not isinstance(inc["version"], int) or inc["version"] < 1:
                return False, _error_response(
                    "invalid_input", f"Include at index {i} version must be a positive integer or null", 400
                )
    return True, None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.route("/healthz", methods=["GET"])
def healthz():
    return _json_response({"ok": True}, 200)


@app.route("/v1/configs/<name>", methods=["POST"])
def create_config(name: str):
    if not name:
        return _error_response("invalid_input", "Config name cannot be empty", 400)

    data, err = _parse_body()
    if err:
        return err

    scope, err = _validate_scope(data)
    if err:
        return err

    if "config" not in data:
        return _error_response("invalid_input", "Missing required field: config", 400)

    config = data["config"]
    if not isinstance(config, dict):
        return _error_response("invalid_input", "'config' must be a JSON object", 400)

    includes = data.get("includes")
    if includes is not None:
        if not isinstance(includes, list):
            return _error_response("invalid_input", "'includes' must be a list", 400)
        ok, err = _validate_includes(includes)
        if not ok:
            return err

    inherits_active = data.get("inherits_active", False)
    if not isinstance(inherits_active, bool):
        return _error_response("invalid_input", "'inherits_active' must be a boolean", 400)

    # Compute body hash for idempotency
    body_hash = hashlib.sha256(request.get_data()).hexdigest()

    result, error, status = store.create(name, scope, config, includes, inherits_active, body_hash)

    if error:
        return _json_response(error, status)

    if status is None:
        # Idempotent hit - return 201 with existing data
        return _json_response(result, 201)

    return _json_response(result, status)


@app.route("/v1/configs/<name>:versions", methods=["POST"])
def list_versions(name: str):
    data, err = _parse_body()
    if err:
        return err

    scope, err = _validate_scope(data)
    if err:
        return err

    result, status = store.list_versions(name, scope)
    return _json_response(result, status)


@app.route("/v1/configs/<name>/<int:version>", methods=["POST"])
def get_version(name: str, version: int):
    data, err = _parse_body()
    if err:
        return err

    scope, err = _validate_scope(data)
    if err:
        return err

    result, status = store.get_version(name, scope, version)
    if result is None:
        return _error_response("not_found", f"Version {version} not found for config '{name}'", 404)
    return _json_response(result, status)


@app.route("/v1/configs/<name>:active", methods=["POST"])
def get_active(name: str):
    data, err = _parse_body()
    if err:
        return err

    scope, err = _validate_scope(data)
    if err:
        return err

    result, status = store.get_active(name, scope)
    if result is None:
        return _error_response("not_found", f"No active version for config '{name}'", 404)
    return _json_response(result, status)


@app.route("/v1/configs/<name>/<int:version>:activate", methods=["POST"])
def activate_version(name: str, version: int):
    data, err = _parse_body()
    if err:
        return err

    scope, err = _validate_scope(data)
    if err:
        return err

    result, status = store.activate(name, scope, version)
    if result is None:
        if status == 409:
            return _error_response("conflict", f"Cannot activate version {version}", 409)
        return _error_response("not_found", f"Version {version} not found for config '{name}'", 404)
    return _json_response(result, status)


@app.route("/v1/configs/<name>:rollback", methods=["POST"])
def rollback(name: str):
    data, err = _parse_body()
    if err:
        return err

    scope, err = _validate_scope(data)
    if err:
        return err

    to_version = data.get("to_version")
    if to_version is None:
        return _error_response("invalid_input", "Missing required field: to_version", 400)
    if not isinstance(to_version, int) or to_version < 1:
        return _error_response("invalid_input", "'to_version' must be a positive integer", 400)

    result, status = store.rollback(name, scope, to_version)
    if result is None:
        if status == 409:
            return _error_response(
                "conflict",
                f"Cannot rollback to version {to_version}: must be <= current active version",
                409,
            )
        return _error_response("not_found", f"Version {to_version} not found for config '{name}'", 404)
    return _json_response(result, status)


@app.route("/v1/configs/<name>:resolve", methods=["POST"])
def resolve(name: str):
    data, err = _parse_body()
    if err:
        return err

    scope, err = _validate_scope(data)
    if err:
        return err

    version = data.get("version")
    if version is not None and (not isinstance(version, int) or version < 1):
        return _error_response("invalid_input", "'version' must be a positive integer or null", 400)

    dry_run = data.get("dry_run", False)
    if not isinstance(dry_run, bool):
        return _error_response("invalid_input", "'dry_run' must be a boolean", 400)

    result, error, status = store.resolve(name, scope, version, dry_run)
    if error:
        return _json_response(error, status)
    return _json_response(result, status)


@app.errorhandler(413)
def request_entity_too_large(e):
    return _error_response("too_large", "Request body exceeds 1 MiB limit", 413)


@app.errorhandler(404)
def not_found(e):
    return _error_response("not_found", "Endpoint not found", 404)


@app.errorhandler(405)
def method_not_allowed(e):
    return _error_response("invalid_input", "Method not allowed", 405)


@app.errorhandler(500)
def internal_error(e):
    return _error_response("internal", "Internal server error", 500)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Configuration server")
    parser.add_argument("--address", default="0.0.0.0", help="Bind address")
    parser.add_argument("--port", type=int, default=8080, help="Bind port")
    args = parser.parse_args()

    app.run(host=args.address, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
