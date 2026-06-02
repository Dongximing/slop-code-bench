#!/usr/bin/env python3
"""Command-line code searcher for Python, JavaScript, and C++ codebases."""

import argparse
import json
import os
import re
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple, Set

import tree_sitter_python as tspython
import tree_sitter_javascript as tsjs
import tree_sitter_cpp as tscpp
from tree_sitter import Language, Parser, Node

_REGEX_FLAGS = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}

_EXTENSION_TO_LANGUAGE = {
    ".py": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp",
    ".hh": "cpp", ".hpp": "cpp", ".hxx": "cpp",
}

_ALL_LANGUAGES = ["python", "javascript", "cpp"]

_LANGUAGE_MODULES = {
    "python": tspython,
    "javascript": tsjs,
    "cpp": tscpp,
}

_PARSERS: Dict[str, Parser] = {}


def get_parser(language: str) -> Parser:
    if language not in _PARSERS:
        lang_module = _LANGUAGE_MODULES[language]
        _PARSERS[language] = Parser(Language(lang_module.language()))
    return _PARSERS[language]


def _line_col(content: str, pos: int) -> tuple[int, int]:
    before = content[:pos]
    line = before.count('\n') + 1
    col = len(before) - before.rfind('\n')
    return line, col


def _extract_metavariables(pattern: str) -> Tuple[str, Dict[str, bool], Set[str]]:
    """Replace $NAME and $NAME? placeholders with valid identifiers.

    Returns (transformed_pattern, {name: is_optional}, all_names).
    """
    meta_vars: Dict[str, bool] = {}
    all_names: Set[str] = set()

    def replace_meta(match: re.Match) -> str:
        name = match.group(1)
        optional = match.group(2) == '?'
        all_names.add(name)
        if name not in meta_vars:
            meta_vars[name] = optional
        else:
            meta_vars[name] = meta_vars[name] or optional
        return f"__META_{name}__"

    pattern = pattern.replace("$$", "__DOLLAR_DOLLAR__")
    pattern = re.sub(r'\$([A-Za-z_][A-Za-z0-9_]*)(\?)?', replace_meta, pattern)
    return pattern, meta_vars, all_names


def _parse_pattern(pattern: str, language: str) -> Tuple[Optional[Node], Set[str], Dict[str, bool]]:
    """Parse a pattern string into an AST node, returning (node, meta_names, is_optional)."""
    transformed, is_optional, meta_names = _extract_metavariables(pattern)
    parser = get_parser(language)
    tree = parser.parse(transformed.encode('utf-8'))

    root = tree.root_node
    if root.child_count == 0:
        return None, meta_names, is_optional

    # Find first non-ERROR child
    content_node = None
    for child in root.children:
        if child.type != 'ERROR':
            content_node = child
            break

    if content_node is None:
        content_node = root

    # Unwrap expression_statement wrapper
    if content_node.type == 'expression_statement' and content_node.child_count > 0:
        content_node = content_node.children[0]

    return content_node, meta_names, is_optional


def _is_meta_placeholder(text: str) -> Optional[str]:
    """If text is a metavar placeholder, return the original name; else None."""
    if text == "__DOLLAR_DOLLAR__":
        return "$$"
    if text.startswith("__META_") and text.endswith("__"):
        name = text[7:-2]
        if name and all(c.isalnum() or c == '_' for c in name):
            return name
    return None


def _match_pattern(
    pattern_node: Node,
    source_node: Node,
    source_bytes: bytes,
    bindings: Dict[str, Tuple[str, List[Node]]],
    meta_names: Set[str],
    is_optional: Dict[str, bool]
) -> bool:
    """Try to match a pattern AST node against a source AST node, updating bindings."""
    p_text = pattern_node.text.decode('utf-8', errors='replace')
    s_text = source_node.text.decode('utf-8', errors='replace')

    meta_name = _is_meta_placeholder(p_text)

    if meta_name:
        if meta_name == "$$":
            return s_text == "$"
        if meta_name in bindings:
            return s_text == bindings[meta_name][0]
        bindings[meta_name] = (s_text, [source_node])
        return True

    # Type compatibility check
    if pattern_node.type == 'ERROR':
        pass  # attempt to continue matching children
    elif source_node.type == 'ERROR':
        return False
    elif pattern_node.type != source_node.type:
        if not (pattern_node.type in ('identifier', 'property_identifier', 'type_identifier') and
                source_node.type in ('identifier', 'property_identifier', 'type_identifier', 'print')):
            return False

    if pattern_node.child_count == 0:
        return p_text == s_text

    return _match_children_recursive(
        list(pattern_node.children), 0,
        list(source_node.children), 0,
        source_bytes, bindings, meta_names, is_optional
    )


def _match_children_recursive(
    p_children: List[Node],
    p_idx: int,
    s_children: List[Node],
    s_idx: int,
    source_bytes: bytes,
    bindings: Dict[str, Tuple[str, List[Node]]],
    meta_names: Set[str],
    is_optional: Dict[str, bool]
) -> bool:
    """Recursive child matching with backtracking for optional metavariables."""
    if p_idx >= len(p_children) and s_idx >= len(s_children):
        return True
    if p_idx >= len(p_children):
        return False
    if s_idx >= len(s_children):
        # All remaining pattern children must be optional metavariables
        for i in range(p_idx, len(p_children)):
            p_text = p_children[i].text.decode('utf-8', errors='replace')
            mn = _is_meta_placeholder(p_text)
            if not (mn and mn in meta_names and is_optional.get(mn, False)):
                return False
        return True

    p_node = p_children[p_idx]
    s_node = s_children[s_idx]

    p_text = p_node.text.decode('utf-8', errors='replace')
    meta_name = _is_meta_placeholder(p_text)

    # Optional metavariable: try skipping it first, then matching it
    if meta_name and meta_name != "$$" and is_optional.get(meta_name, False):
        saved_bindings = {k: v for k, v in bindings.items()}
        if _match_children_recursive(
            p_children, p_idx + 1, s_children, s_idx,
            source_bytes, bindings, meta_names, is_optional
        ):
            return True
        bindings.clear()
        bindings.update(saved_bindings)

    # Non-$$ metavariable binding
    if meta_name and meta_name != "$$":
        s_text = s_node.text.decode('utf-8', errors='replace')

        if meta_name in bindings:
            if bindings[meta_name][0] == s_text:
                bindings[meta_name] = (bindings[meta_name][0], bindings[meta_name][1] + [s_node])
                if _match_children_recursive(
                    p_children, p_idx + 1, s_children, s_idx + 1,
                    source_bytes, bindings, meta_names, is_optional
                ):
                    return True
                bindings[meta_name] = (bindings[meta_name][0], bindings[meta_name][1][:-1])
            return False

        bindings[meta_name] = (s_text, [s_node])
        if _match_children_recursive(
            p_children, p_idx + 1, s_children, s_idx + 1,
            source_bytes, bindings, meta_names, is_optional
        ):
            return True
        del bindings[meta_name]
        return False

    # $$ metavariable
    if meta_name == "$$":
        s_text = s_node.text.decode('utf-8', errors='replace')
        if s_text != "$":
            return False
        return _match_children_recursive(
            p_children, p_idx + 1, s_children, s_idx + 1,
            source_bytes, bindings, meta_names, is_optional
        )

    # Structural node matching
    if p_node.type != s_node.type and not (
        p_node.type in ('identifier', 'property_identifier', 'type_identifier') and
        s_node.type in ('identifier', 'property_identifier', 'type_identifier', 'print')
    ):
        return False

    saved_bindings = {k: v for k, v in bindings.items()}
    if _match_pattern(p_node, s_node, source_bytes, bindings, meta_names, is_optional):
        if _match_children_recursive(
            p_children, p_idx + 1, s_children, s_idx + 1,
            source_bytes, bindings, meta_names, is_optional
        ):
            return True

    bindings.clear()
    bindings.update(saved_bindings)
    return False


def _find_all_nodes(root: Node) -> List[Node]:
    nodes = []

    def traverse(node: Node):
        nodes.append(node)
        for child in node.children:
            traverse(child)

    traverse(root)
    return nodes


def _find_pattern_matches(
    content: str,
    pattern: str,
    language: str,
    rule_id: str
) -> List[Dict[str, Any]]:
    parser = get_parser(language)
    source_bytes = content.encode('utf-8')
    tree = parser.parse(source_bytes)

    pattern_node, meta_names, is_optional = _parse_pattern(pattern, language)
    if pattern_node is None:
        return []

    matches = []
    for source_node in _find_all_nodes(tree.root_node):
        if source_node.type in ('module', 'program', 'translation_unit'):
            continue

        bindings: Dict[str, Tuple[str, List[Node]]] = {}
        if _match_pattern(pattern_node, source_node, source_bytes, bindings, meta_names, is_optional):
            start_byte = source_node.start_byte
            end_byte = source_node.end_byte
            match_text = content[start_byte:end_byte]

            start_line, start_col = _line_col(content, start_byte)
            end_line, end_col = _line_col(content, end_byte)

            captures = {}
            for mn in sorted(bindings):
                bound_text, nodes = bindings[mn]
                ranges = []
                for node in nodes:
                    ns_l, ns_c = _line_col(content, node.start_byte)
                    ne_l, ne_c = _line_col(content, node.end_byte)
                    ranges.append({
                        "start": {"line": ns_l, "col": ns_c},
                        "end": {"line": ne_l, "col": ne_c}
                    })
                captures[f"${mn}"] = {"text": bound_text, "ranges": ranges}

            matches.append({
                "rule_id": rule_id,
                "file": "",
                "language": language,
                "start": {"line": start_line, "col": start_col},
                "end": {"line": end_line, "col": end_col},
                "match": match_text,
                "captures": captures
            })

    return matches


def _prepare_rules(rules: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    prepared = []
    for rule in rules:
        entry = {
            "id": rule["id"],
            "kind": rule["kind"],
            "languages": rule.get("languages", _ALL_LANGUAGES),
        }
        if rule["kind"] == "regex":
            flags = 0
            for f in rule.get("regex_flags", []):
                flags |= _REGEX_FLAGS.get(f, 0)
            entry["compiled"] = re.compile(rule["pattern"], flags)
        else:
            entry["pattern"] = rule["pattern"]
        prepared.append(entry)
    return prepared


def _find_matches(
    content: str,
    prepared: Dict[str, Any],
    file_path: str,
    language: str
) -> List[Dict[str, Any]]:
    rule_id = prepared["id"]
    kind = prepared["kind"]

    if kind == "pattern":
        matches = _find_pattern_matches(content, prepared["pattern"], language, rule_id)
        for m in matches:
            m["file"] = file_path
        return matches

    if kind == "regex":
        hits = [(m.start(), m.end(), m.group()) for m in prepared["compiled"].finditer(content)]
    else:  # exact
        pattern = prepared["pattern"]
        hits = []
        start = 0
        while True:
            pos = content.find(pattern, start)
            if pos == -1:
                break
            hits.append((pos, pos + len(pattern), pattern))
            start = pos + 1

    matches = []
    for start, end, text in hits:
        sl, sc = _line_col(content, start)
        el, ec = _line_col(content, end)
        matches.append({
            "rule_id": rule_id, "file": file_path, "language": language,
            "start": {"line": sl, "col": sc}, "end": {"line": el, "col": ec},
            "match": text,
        })
    return matches


def scan_file(
    file_path: str,
    full_path: Path,
    language: str,
    rules: List[Dict[str, Any]],
    encoding: str
) -> List[Dict[str, Any]]:
    try:
        with open(full_path, "r", encoding=encoding) as f:
            content = f.read()
    except (UnicodeDecodeError, OSError):
        return []

    all_matches = []
    for rule in rules:
        if language in rule["languages"]:
            all_matches.extend(_find_matches(content, rule, file_path, language))
    return all_matches


def main():
    parser = argparse.ArgumentParser(description="Code searcher for Python, JavaScript, and C++ codebases")
    parser.add_argument("root_dir", help="Path to the codebase to scan")
    parser.add_argument("--rules", required=True, help="Path to a JSON array of rules")
    parser.add_argument("--encoding", default="utf-8", help="File encoding (default: utf-8)")
    args = parser.parse_args()

    root_dir = Path(args.root_dir).resolve()
    with open(args.rules, "r", encoding="utf-8") as f:
        raw_rules = json.load(f)
    rules = _prepare_rules(raw_rules)

    all_matches = []
    for dirpath, _, filenames in os.walk(root_dir):
        for filename in filenames:
            full_path = Path(dirpath) / filename
            language = _EXTENSION_TO_LANGUAGE.get(full_path.suffix.lower())
            if language is None:
                continue
            rel_path = full_path.relative_to(root_dir).as_posix()
            all_matches.extend(scan_file(rel_path, full_path, language, rules, args.encoding))

    all_matches.sort(key=lambda m: (
        m["file"],
        m["start"]["line"],
        m["start"]["col"],
        m["end"]["line"],
        m["end"]["col"],
        m["rule_id"]
    ))

    for match in all_matches:
        print(json.dumps(match, separators=(',', ':')))


if __name__ == "__main__":
    main()
