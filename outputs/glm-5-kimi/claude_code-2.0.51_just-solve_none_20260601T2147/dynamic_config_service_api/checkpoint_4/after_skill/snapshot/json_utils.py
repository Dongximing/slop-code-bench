"""JSON Pointer utilities and JSON Patch diff computation."""

import json
from copy import deepcopy
from parsers import normalize_value, canonical_json


def escape_json_pointer(s):
    return s.replace('~', '~0').replace('/', '~1')


def unescape_json_pointer(s):
    return s.replace('~1', '/').replace('~0', '~')


def get_value_by_pointer(obj, pointer):
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


def compute_json_patch(old, new, path=''):
    operations = []
    old_normalized = normalize_value(old)
    new_normalized = normalize_value(new)

    if isinstance(old_normalized, dict) and isinstance(new_normalized, dict):
        old_keys = set(old_normalized.keys())
        new_keys = set(new_normalized.keys())

        for key in sorted(old_keys - new_keys):
            operations.append({"op": "remove", "path": path + '/' + escape_json_pointer(key)})

        for key in sorted(new_keys):
            key_path = path + '/' + escape_json_pointer(key)
            if key not in old_keys:
                operations.append({"op": "add", "path": key_path, "value": new_normalized[key]})
            elif old_normalized[key] != new_normalized[key]:
                operations.extend(compute_json_patch(old_normalized[key], new_normalized[key], key_path))

    elif isinstance(old_normalized, list) and isinstance(new_normalized, list):
        if old_normalized == new_normalized:
            return []

        old_len = len(old_normalized)
        new_len = len(new_normalized)

        for i in range(new_len, old_len):
            operations.append({"op": "remove", "path": path + '/' + str(new_len)})

        for i in range(new_len):
            elem_path = path + '/' + str(i)
            if i >= old_len:
                operations.append({"op": "add", "path": elem_path, "value": new_normalized[i]})
            elif old_normalized[i] != new_normalized[i]:
                operations.extend(compute_json_patch(old_normalized[i], new_normalized[i], elem_path))

    elif old_normalized != new_normalized:
        operations.append({"op": "replace", "path": path if path else '/', "value": new_normalized})

    return operations


def sort_patch_operations(ops):
    op_order = {"remove": 0, "replace": 1, "add": 2}
    return sorted(ops, key=lambda op: (op.get("path", ""), op_order.get(op.get("op", ""), 99)))


def _compute_includes_diff(old_includes, new_includes):
    changes = []

    def normalize_include(inc):
        return {
            "name": inc.get("name"),
            "scope": normalize_value(inc.get("scope", {})),
            "version": inc.get("version")
        }

    old_normalized = [normalize_include(inc) for inc in old_includes]
    new_normalized = [normalize_include(inc) for inc in new_includes]
    max_len = max(len(old_normalized), len(new_normalized))

    for i in range(max_len):
        if i >= len(old_normalized):
            changes.append({"op": "add", "index": i, "ref": new_includes[i]})
        elif i >= len(new_normalized):
            changes.append({"op": "remove", "index": i, "ref": old_includes[i]})
        elif old_normalized[i] != new_normalized[i]:
            changes.append({
                "op": "update", "index": i,
                "from_version": old_normalized[i]["version"],
                "to_version": new_normalized[i]["version"]
            })

    return changes


_HUMAN_SORT_ORDER = {"DELETE": 0, "REPLACE": 1, "SET": 2,
                     "INCLUDE_ADD": 3, "INCLUDE_REMOVE": 4, "INCLUDE_UPDATE": 5}


def _compute_human_diff(raw_patch, includes_changes):
    lines = []

    for op in raw_patch:
        path = op.get("path", "")
        operation = op.get("op")
        if operation == "remove":
            lines.append(f"DELETE {path}")
        elif operation == "replace":
            lines.append(f"REPLACE {path}: {canonical_json(op.get('value'))}")
        elif operation == "add":
            lines.append(f"SET {path}: {canonical_json(op.get('value'))}")

    for change in includes_changes:
        op_type = change.get("op")
        index = change.get("index")
        if op_type == "add":
            ref = change.get("ref", {})
            name = ref.get("name", "")
            scope_json = canonical_json(ref.get("scope", {}))
            version = ref.get("version")
            lines.append(f"INCLUDE_ADD [{index}] {name}@{scope_json} v={canonical_json(version)}")
        elif op_type == "remove":
            ref = change.get("ref", {})
            name = ref.get("name", "")
            scope_json = canonical_json(ref.get("scope", {}))
            version = ref.get("version")
            lines.append(f"INCLUDE_REMOVE [{index}] {name}@{scope_json} v={canonical_json(version)}")
        elif op_type == "update":
            lines.append(
                f"INCLUDE_UPDATE [{index}]: "
                f"{canonical_json(change.get('from_version'))} -> {canonical_json(change.get('to_version'))}")

    def sort_key(line):
        for prefix, order in _HUMAN_SORT_ORDER.items():
            if line.startswith(prefix):
                return (order, line)
        return (len(_HUMAN_SORT_ORDER), line)

    return sorted(lines, key=sort_key)


def compute_diffs(old_config, new_config, old_resolved, new_resolved,
                  old_includes, new_includes):
    raw_patch = sort_patch_operations(compute_json_patch(old_config, new_config))
    resolved_patch = sort_patch_operations(compute_json_patch(old_resolved, new_resolved))
    includes_changes = _compute_includes_diff(old_includes, new_includes)
    human = _compute_human_diff(raw_patch, includes_changes)

    return {
        "raw_json_patch": raw_patch,
        "resolved_json_patch": resolved_patch,
        "includes_changes": includes_changes,
        "human": human
    }


def deep_merge(base, override):
    result = deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result
