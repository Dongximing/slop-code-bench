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

# Cache for parsers
_PARSERS: Dict[str, Parser] = {}


def get_parser(language: str) -> Parser:
    """Get or create a parser for the given language."""
    if language not in _PARSERS:
        lang_module = _LANGUAGE_MODULES[language]
        _PARSERS[language] = Parser(Language(lang_module.language()))
    return _PARSERS[language]


def _line_col(content: str, pos: int) -> tuple[int, int]:
    """Convert byte position to (line, col) with 1-based coordinates."""
    before = content[:pos]
    line = before.count('\n') + 1
    col = len(before) - before.rfind('\n')
    return line, col


def _extract_metavariables(pattern: str) -> Tuple[str, Dict[str, bool], Set[str]]:
    """
    Extract metavariables from pattern and replace them with valid identifiers.
    Returns (transformed_pattern, is_optional, all_meta_names).
    - is_optional maps metavar name (without ?) to True if optional
    - all_meta_names contains all metavar names (without ?)
    """
    meta_vars: Dict[str, bool] = {}  # name -> is_optional
    all_names: Set[str] = set()

    def replace_meta(match: re.Match) -> str:
        name = match.group(1)
        optional = match.group(2) == '?'
        all_names.add(name)
        if name not in meta_vars:
            meta_vars[name] = optional
        else:
            # If any occurrence is optional, the var is optional
            meta_vars[name] = meta_vars[name] or optional
        return f"__META_{name}__"

    # Replace $$ with a placeholder first
    pattern = pattern.replace("$$", "__DOLLAR_DOLLAR__")

    # Replace $NAME? or $NAME with placeholder
    # NAME can be alphanumeric + underscore
    pattern = re.sub(r'\$([A-Za-z_][A-Za-z0-9_]*)(\?)?', replace_meta, pattern)

    return pattern, meta_vars, all_names


def _parse_pattern(pattern: str, language: str) -> Tuple[Optional[Node], Set[str], Dict[str, bool]]:
    """
    Parse a pattern string and return the relevant AST node(s).
    Returns (pattern_node, meta_names, is_optional).
    """
    transformed, is_optional, meta_names = _extract_metavariables(pattern)
    parser = get_parser(language)
    tree = parser.parse(transformed.encode('utf-8'))

    # Get the root node - skip module/program wrapper
    root = tree.root_node
    if root.child_count == 0:
        return None, meta_names, is_optional

    # For most languages, the first child is the actual content
    # module -> expression_statement -> call (Python)
    # program -> expression_statement -> call_expression (JS)
    # translation_unit -> declaration (C++)

    # We need to find the "content" node - skip ERROR nodes at the start
    content_node = None
    for child in root.children:
        if child.type != 'ERROR':
            content_node = child
            break

    if content_node is None:
        # If all children are ERROR, use the root
        content_node = root

    # For expression_statement, get the inner expression
    if content_node.type in ('expression_statement',):
        if content_node.child_count > 0:
            content_node = content_node.children[0]

    return content_node, meta_names, is_optional


def _is_meta_placeholder(text: str) -> Optional[str]:
    """Check if text is a metavar placeholder and return the original name."""
    if text == "__DOLLAR_DOLLAR__":
        return "$$"
    if text.startswith("__META_") and text.endswith("__"):
        # Extract the name part
        name = text[7:-2]
        # Only valid if it's a simple identifier (no spaces, operators, etc.)
        if name and all(c.isalnum() or c == '_' for c in name):
            return name
    return None


def _get_meta_info(text: str) -> Tuple[bool, Optional[str]]:
    """
    Check if a node represents a metavariable.
    Returns (is_meta, original_name).
    """
    meta_name = _is_meta_placeholder(text)
    if meta_name:
        return True, meta_name
    return False, None


def _node_text(node: Node, source: bytes) -> str:
    """Get the text of a node from source bytes."""
    return node.text.decode('utf-8', errors='replace')


def _match_pattern(
    pattern_node: Node,
    source_node: Node,
    source_bytes: bytes,
    bindings: Dict[str, Tuple[str, List[Node]]],
    meta_names: Set[str],
    is_optional: Dict[str, bool]
) -> bool:
    """
    Try to match a pattern AST node against a source AST node.
    Updates bindings dict with matched metavariables.
    Returns True if match succeeds.
    """
    p_text = _node_text(pattern_node, b'')
    s_text = _node_text(source_node, source_bytes)

    # Check if pattern node is a metavariable
    is_meta, meta_name = _get_meta_info(p_text)

    if is_meta:
        if meta_name == "$$":
            # $$ matches literal $ in source
            return s_text == "$"

        # Check if this metavariable is already bound
        if meta_name in bindings:
            # Must match the same text
            bound_text = bindings[meta_name][0]
            return s_text == bound_text
        else:
            # Bind this metavariable to the source node's text
            bindings[meta_name] = (s_text, [source_node])
            return True

    # If pattern has ERROR but source doesn't at this position, it might still match
    # This can happen when metavariables cause parse errors in pattern

    # Check node types
    if pattern_node.type == 'ERROR':
        # For ERROR nodes in pattern, try to match children flexibly
        # This happens when pattern has $X($Y) style
        pass
    elif source_node.type == 'ERROR':
        # Source has ERROR - usually means syntax error, skip
        return False
    elif pattern_node.type != source_node.type:
        # Allow matching different types if the pattern is an identifier and source is compatible
        # For example, pattern 'print' (identifier) matching source 'print' (print keyword in Python 2)
        # But for most cases, types should match
        if not (pattern_node.type in ('identifier', 'property_identifier', 'type_identifier') and
                source_node.type in ('identifier', 'property_identifier', 'type_identifier', 'print')):
            return False

    # Check if this is a leaf node (no children)
    if pattern_node.child_count == 0:
        # Leaf node - text must match exactly
        return p_text == s_text

    # For non-leaf nodes, try to match children
    # We need to handle optional metavariables

    p_children = list(pattern_node.children)
    s_children = list(source_node.children)

    # Try to match children, allowing optional metavariables to be skipped
    return _match_children(
        p_children, s_children, source_bytes, bindings, meta_names, is_optional
    )


def _match_children(
    p_children: List[Node],
    s_children: List[Node],
    source_bytes: bytes,
    bindings: Dict[str, Tuple[str, List[Node]]],
    meta_names: Set[str],
    is_optional: Dict[str, bool]
) -> bool:
    """
    Match pattern children against source children.
    Handles optional metavariables and flexible matching.
    """
    if not p_children and not s_children:
        return True

    if not p_children:
        return not s_children

    # Try to match with backtracking for optional metavariables
    return _match_children_recursive(
        p_children, 0, s_children, 0, source_bytes, bindings, meta_names, is_optional
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
    """Recursive matching with backtracking for optional metavariables."""
    # Base cases
    if p_idx >= len(p_children) and s_idx >= len(s_children):
        return True
    if p_idx >= len(p_children):
        # Pattern exhausted but source has more children
        return False
    if s_idx >= len(s_children):
        # Source exhausted - check if remaining pattern children are optional
        for i in range(p_idx, len(p_children)):
            p_text = _node_text(p_children[i], b'')
            is_meta, meta_name = _get_meta_info(p_text)
            if is_meta and meta_name in meta_names:
                if not is_optional.get(meta_name, False):
                    return False
            else:
                # Non-meta, non-optional - must match
                return False
        return True

    p_node = p_children[p_idx]
    s_node = s_children[s_idx]

    p_text = _node_text(p_node, b'')
    is_meta, meta_name = _get_meta_info(p_text)

    # Check if this is an optional metavariable
    if is_meta and meta_name != "$$" and is_optional.get(meta_name, False):
        # Try two paths: skip the optional, or match it
        # Path 1: Skip optional (don't consume source)
        saved_bindings = {k: v for k, v in bindings.items()}
        if _match_children_recursive(
            p_children, p_idx + 1, s_children, s_idx,
            source_bytes, bindings, meta_names, is_optional
        ):
            return True
        # Restore bindings
        bindings.clear()
        bindings.update(saved_bindings)

        # Path 2: Match the optional
        # Fall through to normal matching

    # Check for metavariable at this position
    if is_meta and meta_name != "$$":
        s_text = _node_text(s_node, source_bytes)

        if meta_name in bindings:
            # Already bound - must match same text
            if bindings[meta_name][0] == s_text:
                # Add this occurrence
                bindings[meta_name] = (bindings[meta_name][0], bindings[meta_name][1] + [s_node])
                if _match_children_recursive(
                    p_children, p_idx + 1, s_children, s_idx + 1,
                    source_bytes, bindings, meta_names, is_optional
                ):
                    return True
                # Backtrack
                bindings[meta_name] = (bindings[meta_name][0], bindings[meta_name][1][:-1])
                return False
            else:
                return False
        else:
            # New binding
            bindings[meta_name] = (s_text, [s_node])
            if _match_children_recursive(
                p_children, p_idx + 1, s_children, s_idx + 1,
                source_bytes, bindings, meta_names, is_optional
            ):
                return True
            # Backtrack
            del bindings[meta_name]
            return False

    if is_meta and meta_name == "$$":
        s_text = _node_text(s_node, source_bytes)
        if s_text != "$":
            return False
        return _match_children_recursive(
            p_children, p_idx + 1, s_children, s_idx + 1,
            source_bytes, bindings, meta_names, is_optional
        )

    # Normal matching - try to match this pair of nodes
    # First check types
    if p_node.type == 'ERROR':
        # Pattern has ERROR - try to match children
        if _match_children_recursive(
            list(p_node.children), 0, s_children, s_idx,
            source_bytes, bindings, meta_names, is_optional
        ):
            # Consumed some source nodes - find how many
            # This is tricky, need to count how many source nodes were consumed
            # For simplicity, try to match pattern ERROR children with sequential source children
            pass

    if p_node.type != s_node.type and not (
        p_node.type in ('identifier', 'property_identifier', 'type_identifier') and
        s_node.type in ('identifier', 'property_identifier', 'type_identifier', 'print')
    ):
        # Types don't match - but check if pattern is an ERROR that we can handle
        if p_node.type == 'ERROR':
            # Try to match ERROR's children with current source
            # This handles cases like $X($Y) where $ causes ERROR
            p_error_children = list(p_node.children)
            if len(p_error_children) >= 2:
                # Often ERROR has $ followed by identifier
                # Try to match them as a metavariable sequence
                pass

        return False

    # Try to match the nodes
    saved_bindings = {k: v for k, v in bindings.items()}

    if _match_pattern(p_node, s_node, source_bytes, bindings, meta_names, is_optional):
        if _match_children_recursive(
            p_children, p_idx + 1, s_children, s_idx + 1,
            source_bytes, bindings, meta_names, is_optional
        ):
            return True

    # Backtrack
    bindings.clear()
    bindings.update(saved_bindings)
    return False


def _find_all_nodes(root: Node) -> List[Node]:
    """Get all nodes in tree in depth-first order."""
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
    """Find all matches of a pattern in content."""
    parser = get_parser(language)
    source_bytes = content.encode('utf-8')
    tree = parser.parse(source_bytes)

    pattern_node, meta_names, is_optional = _parse_pattern(pattern, language)
    if pattern_node is None:
        return []

    matches = []
    source_root = tree.root_node

    # Get all nodes in the source tree
    all_nodes = _find_all_nodes(source_root)

    for source_node in all_nodes:
        # Skip the root module/program node
        if source_node.type in ('module', 'program', 'translation_unit'):
            continue

        bindings: Dict[str, Tuple[str, List[Node]]] = {}

        if _match_pattern(pattern_node, source_node, source_bytes, bindings, meta_names, is_optional):
            # Build match result
            start_byte = source_node.start_byte
            end_byte = source_node.end_byte
            match_text = content[start_byte:end_byte]

            start_line, start_col = _line_col(content, start_byte)
            end_line, end_col = _line_col(content, end_byte)

            # Build captures
            captures = {}
            for meta_name in sorted(bindings.keys()):
                bound_text, nodes = bindings[meta_name]
                ranges = []
                for node in nodes:
                    n_start_line, n_start_col = _line_col(content, node.start_byte)
                    n_end_line, n_end_col = _line_col(content, node.end_byte)
                    ranges.append({
                        "start": {"line": n_start_line, "col": n_start_col},
                        "end": {"line": n_end_line, "col": n_end_col}
                    })
                captures[f"${meta_name}"] = {
                    "text": bound_text,
                    "ranges": ranges
                }

            matches.append({
                "rule_id": rule_id,
                "file": "",  # Will be filled by caller
                "language": language,
                "start": {"line": start_line, "col": start_col},
                "end": {"line": end_line, "col": end_col},
                "match": match_text,
                "captures": captures
            })

    return matches


def _prepare_rules(rules: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Prepare rules for matching."""
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
    """Find all matches for a rule in content."""
    rule_id = prepared["id"]
    kind = prepared["kind"]

    if kind == "pattern":
        matches = _find_pattern_matches(content, prepared["pattern"], language, rule_id)
        for m in matches:
            m["file"] = file_path
        return matches

    # Exact and regex matching
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
    """Scan a file for all rule matches."""
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

    # Sort by file, then start position, then end position, then rule_id
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
