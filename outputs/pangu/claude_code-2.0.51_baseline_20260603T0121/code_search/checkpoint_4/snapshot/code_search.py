#!/usr/bin/env python3
"""Command-line code searcher for Python codebases with structure-aware pattern matching."""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


def expand_template(template: str, match_text: str, captures: dict[str, Any]) -> str:
    """Expand template with $NAME placeholders and $MATCH."""
    result = []
    i = 0
    while i < len(template):
        if template[i] == '$':
            if i + 1 < len(template):
                if template[i + 1] == '$':
                    # Literal dollar sign
                    result.append('$')
                    i += 2
                    continue
                elif template[i + 1] == 'MATCH':
                    # Full match text
                    result.append(match_text)
                    i += len('MATCH') + 1
                    continue
                else:
                    # Named placeholder
                    j = i + 1
                    # Parse the name: must start with letter or underscore, followed by alphanumeric/underscore
                    if j < len(template) and (template[j].isalpha() or template[j] == '_'):
                        j += 1
                        while j < len(template) and (template[j].isalnum() or template[j] == '_'):
                            j += 1
                        name = template[i+1:j]
                        # Look up in captures
                        capture_value = captures.get(name)
                        if capture_value is not None:
                            if isinstance(capture_value, dict) and 'text' in capture_value:
                                result.append(capture_value['text'])
                            elif isinstance(capture_value, str):
                                result.append(capture_value)
                        else:
                            # Keep the placeholder as-is if not found
                            result.append(template[i:j])
                        i = j
                        continue
            # If we get here, it's a dangling $ at end of string - keep as-is
            result.append('$')
            i += 1
        else:
            result.append(template[i])
            i += 1

    return ''.join(result)

# Try to import tree-sitter modules
try:
    import tree_sitter as ts
    import tree_sitter_python as tspython
    import tree_sitter_javascript as tsjavascript
    import tree_sitter_cpp as tscpp
    HAS_TREE_SITTER = True
except ImportError:
    HAS_TREE_SITTER = False
    ts = None


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Search Python codebase for code patterns with structure-aware matching."
    )
    parser.add_argument(
        "root_dir",
        type=str,
        help="Path to the codebase to scan."
    )
    parser.add_argument(
        "--rules",
        type=str,
        required=True,
        help="Path to a JSON array of rules."
    )
    parser.add_argument(
        "--encoding",
        type=str,
        default="utf-8",
        help="File encoding (default: utf-8)."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without writing to disk."
    )
    parser.add_argument(
        "--apply-fixes",
        action="store_true",
        help="Write changes to disk."
    )
    args = parser.parse_args()

    # Validate mutually exclusive flags
    if args.dry_run and args.apply_fixes:
        parser.error("--dry-run and --apply-fixes are mutually exclusive")

    return args


def load_rules(rules_path: str) -> list[dict[str, Any]]:
    """Load and validate rules from JSON file."""
    with open(rules_path, "r", encoding="utf-8") as f:
        rules = json.load(f)

    if not isinstance(rules, list):
        raise ValueError("Rules must be a JSON array")

    if len(rules) == 0:
        return []

    # Validate each rule
    seen_ids = set()
    for i, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise ValueError(f"Rule at index {i} must be an object")

        # Validate id
        if "id" not in rule or not isinstance(rule["id"], str) or not rule["id"]:
            raise ValueError(f"Rule at index {i} must have a non-empty string id")

        rule_id = rule["id"]
        if rule_id in seen_ids:
            raise ValueError(f"Duplicate rule id: {rule_id}")
        seen_ids.add(rule_id)

        # Validate kind
        if "kind" not in rule or rule["kind"] not in ("exact", "regex", "pattern", "selector"):
            raise ValueError(f"Rule '{rule_id}' must have kind 'exact', 'regex', 'pattern', or 'selector'")

        # Validate pattern or selector depending on kind
        if rule["kind"] == "selector":
            if "selector" not in rule or not isinstance(rule["selector"], str) or not rule["selector"]:
                raise ValueError(f"Rule '{rule_id}' must have a non-empty string selector")
        else:
            if "pattern" not in rule or not isinstance(rule["pattern"], str) or not rule["pattern"]:
                raise ValueError(f"Rule '{rule_id}' must have a non-empty string pattern")

        # Validate languages
        if "languages" in rule:
            langs = rule["languages"]
            if not isinstance(langs, list):
                raise ValueError(f"Rule '{rule_id}' languages must be an array")
            for lang in langs:
                if lang not in ("python", "javascript", "cpp"):
                    raise ValueError(
                        f"Rule '{rule_id}' languages may only contain 'python', 'javascript', 'cpp', got '{lang}'"
                    )

        # Validate regex_flags (only for regex kind)
        if rule["kind"] == "regex" and "regex_flags" in rule:
            flags = rule["regex_flags"]
            if not isinstance(flags, list):
                raise ValueError(f"Rule '{rule_id}' regex_flags must be an array")
            valid_flags = {"i", "m", "s"}
            for flag in flags:
                if flag not in valid_flags:
                    raise ValueError(
                        f"Rule '{rule_id}' has invalid regex flag '{flag}'. "
                        f"Valid flags: i, m, s"
                    )

        # Validate fix object if present
        if "fix" in rule:
            fix = rule["fix"]
            if not isinstance(fix, dict):
                raise ValueError(f"Rule '{rule_id}' fix must be an object")
            if "kind" not in fix or fix["kind"] != "replace":
                raise ValueError(f"Rule '{rule_id}' fix.kind must be 'replace'")
            if "template" not in fix or not isinstance(fix["template"], str):
                raise ValueError(f"Rule '{rule_id}' fix.template must be a string")

    return rules


EXTENSION_TO_LANGUAGE = {
    # Python
    ".py": "python",
    # JavaScript
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    # C++
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hh": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
}


def compile_regex(pattern: str, flags_list: list[str] | None) -> re.Pattern:
    """Compile a regex pattern with the given flags."""
    flags = 0
    if flags_list:
        for flag in flags_list:
            if flag == "i":
                flags |= re.IGNORECASE
            elif flag == "m":
                flags |= re.MULTILINE
            elif flag == "s":
                flags |= re.DOTALL

    return re.compile(pattern, flags)


def find_matches_in_line(
    line: str,
    line_num: int,
    rule: dict[str, Any]
) -> list[dict[str, Any]]:
    """Find all matches of a rule in a single line."""
    matches = []
    pattern = rule["pattern"]
    kind = rule["kind"]

    if kind == "exact":
        # Find all non-overlapping exact matches
        start = 0
        while True:
            pos = line.find(pattern, start)
            if pos == -1:
                break
            matches.append({
                "start": pos,
                "end": pos + len(pattern),
                "match_text": pattern
            })
            start = pos + 1  # Allow overlapping matches
    elif kind == "regex":
        regex = compile_regex(pattern, rule.get("regex_flags"))
        for m in regex.finditer(line):
            matches.append({
                "start": m.start(),
                "end": m.end(),
                "match_text": m.group()
            })

    # Convert to output format
    results = []
    for match in matches:
        results.append({
            "start": {
                "line": line_num + 1,  # 1-based
                "col": match["start"] + 1  # 1-based
            },
            "end": {
                "line": line_num + 1,  # 1-based
                "col": match["end"] + 1  # 1-based, position AFTER match
            },
            "match": match["match_text"]
        })

    return results


class PatternMatcher:
    """Handles pattern matching with metavariables using tree-sitter."""

    LANGUAGE_MAP = {
        "python": (tspython, "Python"),
        "javascript": (tsjavascript, "JavaScript"),
        "cpp": (tscpp, "C++"),
    }

    def __init__(self):
        self._parsers = {}
        self._queries_cache = {}

    def _get_parser(self, language: str):
        """Get or create a parser for the given language."""
        if language in self._parsers:
            return self._parsers[language]

        if language not in self.LANGUAGE_MAP:
            raise ValueError(f"Unsupported language for pattern matching: {language}")

        lang_module, lang_name = self.LANGUAGE_MAP[language]
        parser = ts.Parser()
        parser.language = ts.Language(lang_module.language())
        self._parsers[language] = parser
        return parser

    def _parse_pattern(self, pattern: str, language: str) -> tuple[Any, list[tuple[str, bool]]]:
        """Parse a pattern string and extract metavariables.

        Returns:
            Tuple of (tree, list of (metavariable_name, is_optional) tuples).
        """
        # Extract all metavariables from the pattern
        # Metavariable pattern: $NAME or $NAME? (optional)
        # Need to handle $$ escape for literal $

        # First, replace escaped $$ with a placeholder
        temp_placeholder = "\x00ESCAPED_DOLLAR_SIGN\x00"
        pattern_without_escaped = pattern.replace("$$", temp_placeholder)

        # Find all metavariables
        # Pattern: $(?!\$)[A-Z][A-Z0-9_]*\??
        metavariable_pattern = r'\$([A-Z][A-Z0-9_]*)(\?)?'
        metavariables = []

        def replace_with_capture(match):
            name = match.group(1)
            optional = match.group(2) is not None
            metavariables.append((name, optional))
            return f"\x00META_{name}_{len(metavariables)-1}\x00"

        pattern_with_markers = re.sub(metavariable_pattern, replace_with_capture, pattern_without_escaped)

        # Restore escaped dollar signs
        pattern_final = pattern_with_markers.replace(temp_placeholder, "$")

        # Parse the pattern
        parser = self._get_parser(language)
        tree = parser.parse(bytes(pattern_final, "utf-8"))

        return tree, metavariables

    def _generate_query(self, pattern_tree: ts.Tree, language: str) -> str:
        """Generate a tree-sitter query from the pattern tree."""
        # For now, we'll use a simple approach: create a query that captures
        # all nodes at the same positions as our metavariables
        # This is a simplified approach - in practice we'd need to traverse
        # the tree and build a proper query

        # The query should match the entire root node of the pattern
        # and capture metavariable positions
        lang_module, _ = self.LANGUAGE_MAP[language]
        lang = lang_module.language()

        # Get the root node type
        root_node = pattern_tree.root_node

        # Generate query: capture all nodes in the pattern structure
        # We'll use a field-aware approach
        return self._node_to_query(root_node, pattern_tree)

    def _node_to_query(self, node: ts.Node, pattern_tree: ts.Tree) -> str:
        """Convert a tree-sitter node to a query pattern."""
        # Get node type
        node_type = node.type

        # Check if this node contains a metavariable marker
        text = node.text.decode("utf-8")
        meta_match = re.match(r'\x00META_([A-Z][A-Z0-9_]*)_(\d+)\x00', text)

        if meta_match:
            # This node is a metavariable placeholder
            meta_name = meta_match.group(1)
            return f'({node_type}) @${meta_name}'

        # For simple nodes, return the node type
        return node_type

    def _find_node_at_position(self, tree: ts.Tree, line: int, col: int) -> ts.Node:
        """Find the node at a given (line, col) position (1-based)."""
        # Convert 1-based to 0-based
        point = ts.Point(line - 1, col - 1)
        return tree.root_node.named_node_at_point(point)

    def _get_node_text(self, node: ts.Node, source: bytes) -> str:
        """Get the text of a node."""
        return source[node.start_byte:node.end_byte].decode("utf-8")

    def _node_to_position(self, node: ts.Node) -> dict[str, dict[str, int]]:
        """Convert a node to position dict."""
        return {
            "start": {
                "line": node.start_point[0] + 1,
                "col": node.start_point[1] + 1
            },
            "end": {
                "line": node.end_point[0] + 1,
                "col": node.end_point[1] + 1
            }
        }

    def match(
        self,
        pattern: str,
        language: str,
        source: bytes
    ) -> list[dict[str, Any]]:
        """Match a pattern against source code and return all matches."""
        if not HAS_TREE_SITTER:
            return []

        # Parse the pattern
        pattern_tree, metavariables = self._parse_pattern(pattern, language)

        # Parse the source
        parser = self._get_parser(language)
        source_tree = parser.parse(source)

        # Simple approach: do structural comparison between pattern and source
        return self._pattern_match_nodes(
            pattern_tree.root_node,
            source_tree.root_node,
            source,
            metavariables,
            language
        )

    def _pattern_match_nodes(
        self,
        pattern_node: ts.Node,
        source_node: ts.Node,
        source: bytes,
        metavariables: list[tuple[str, bool]],
        language: str
    ) -> list[dict[str, Any]]:
        """Recursively match pattern nodes to source nodes."""
        matches = []

        # Check if nodes match structurally
        pattern_type = pattern_node.type
        source_type = source_node.type

        # Special handling for some node types
        if not self._node_types_match(pattern_type, source_type, language):
            return matches

        # Check children count
        if len(pattern_node.children) != len(source_node.children):
            return matches

        # Check each child
        match_info = {
            "captures": {},
            "source_node": source_node,
            "pattern_node": pattern_node
        }

        all_children_match = True
        for i, pattern_child in enumerate(pattern_node.children):
            source_child = source_node.children[i]
            child_match = self._match_single_node(
                pattern_child, source_child, source, metavariables, match_info
            )
            if not child_match:
                all_children_match = False
                break

        if all_children_match:
            # Build the match result
            match_text = self._get_node_text(source_node, source)
            position = self._node_to_position(source_node)

            # Build captures dict
            captures = {}
            for meta_name, meta_pos in match_info.get("captures", {}).items():
                meta_node = source_node.children[meta_pos]
                meta_text = self._get_node_text(meta_node, source)
                meta_position = self._node_to_position(meta_node)
                captures[meta_name] = {
                    "text": meta_text,
                    "ranges": [meta_position]
                }

            matches.append({
                "rule_id": "",
                "file": "",
                "language": language,
                "start": position["start"],
                "end": position["end"],
                "match": match_text,
                "captures": captures
            })

        # Also check siblings
        for sibling in source_node.next_sibling:
            sibling_matches = self._pattern_match_nodes(
                pattern_node, sibling, source, metavariables, language
            )
            matches.extend(sibling_matches)

        return matches

    def _match_single_node(
        self,
        pattern_node: ts.Node,
        source_node: ts.Node,
        source: bytes,
        metavariables: list[tuple[str, bool]],
        match_info: dict
    ) -> bool:
        """Match a single pattern node to a source node."""
        pattern_text = pattern_node.text.decode("utf-8")

        # Check for metavariable marker
        meta_match = re.match(r'\x00META_([A-Z][A-Z0-9_]*)_(\d+)\x00', pattern_text)

        if meta_match:
            # This is a metavariable placeholder
            meta_name = meta_match.group(1)
            meta_index = int(meta_match.group(2))
            meta_name_actual, _ = metavariables[meta_index]

            # Record this capture
            if meta_name_actual not in match_info["captures"]:
                match_info["captures"][meta_name_actual] = pattern_node.child_index

            return True

        # Regular node comparison
        pattern_type = pattern_node.type
        source_type = source_node.type

        if not self._node_types_match(pattern_type, source_type, language):
            return False

        # Compare text if both are named nodes
        if pattern_node.is_named and source_node.is_named:
            pattern_text_actual = self._get_node_text(pattern_node, None)
            source_text_actual = self._get_node_text(source_node, source)

            # For identifiers and literals, text must match exactly
            if pattern_type in ("identifier", "string", "number", "character_literal"):
                if pattern_text_actual != source_text_actual:
                    return False

        return True

    def _node_types_match(self, pattern_type: str, source_type: str, language: str) -> bool:
        """Check if pattern node type matches source node type."""
        # Allow some flexibility in matching
        # For example, any expression can match at an expression position
        if pattern_type == source_type:
            return True

        # Check type hierarchy (e.g., expression matches at expression position)
        type_equivalences = {
            "python": {
                "expression": ["argument", "call", "attribute", "subscript", "binary_operator", "unary_operator"],
                "term": ["identifier", "number", "string", "none", "true", "false"],
            },
            "javascript": {
                "expression": ["call_expression", "member_expression", "binary_expression", "unary_expression", "literal"],
                "primary": ["identifier", "literal"],
            },
            "cpp": {
                "expression": ["call_expression", "member_access_expression", "binary_expression", "unary_expression", "literal"],
                "primary_expression": ["identifier", "literal"],
            }
        }

        lang_equivs = type_equivalences.get(language, {})
        if pattern_type in lang_equivs:
            return source_type in lang_equivs[pattern_type]

        return False


def search_file(
    file_path: Path,
    root_dir: Path,
    rules: list[dict[str, Any]],
    encoding: str
) -> list[dict[str, Any]]:
    """Search a single file for all rule matches."""
    all_matches = []

    try:
        with open(file_path, "r", encoding=encoding) as f:
            lines = f.readlines()
    except UnicodeDecodeError:
        # Skip files that fail to decode
        return all_matches

    # Compute relative path with '/' separators
    rel_path = file_path.relative_to(root_dir)
    file_str = rel_path.as_posix()

    # Determine language from file extension
    suffix = file_path.suffix
    language = EXTENSION_TO_LANGUAGE.get(suffix)
    if language is None:
        # Not a supported file type
        return all_matches

    # Read file as bytes for tree-sitter
    with open(file_path, "rb") as f:
        source_bytes = f.read()

    for line_num, line in enumerate(lines):
        for rule in rules:
            # Check if rule applies to this language
            languages = rule.get("languages")
            if languages is not None and language not in languages:
                continue

            if rule["kind"] == "pattern":
                if HAS_TREE_SITTER:
                    # Use pattern matcher for line matches
                    matches = find_pattern_matches_in_line(
                        line, line_num, rule, language, source_bytes
                    )
                    for match in matches:
                        match["rule_id"] = rule["id"]
                        match["file"] = file_str
                        match["language"] = language
                        all_matches.append(match)
            elif rule["kind"] == "selector":
                # Selector matches are not line-based
                if HAS_TREE_SITTER:
                    matches = find_selector_matches(file_path, rule, language, source_bytes)
                    # Filter to matches in this line
                    for match in matches:
                        if match["start"]["line"] == line_num + 1:
                            match["rule_id"] = rule["id"]
                            match["file"] = file_str
                            match["language"] = language
                            all_matches.append(match)
            else:
                # Use existing exact/regex matching
                matches = find_matches_in_line(line, line_num, rule)
                for match in matches:
                    match["file"] = file_str
                    match["language"] = language
                    all_matches.append(match)

    return all_matches


def find_pattern_matches_in_line(
    line: str,
    line_num: int,
    rule: dict[str, Any],
    language: str,
    source: bytes
) -> list[dict[str, Any]]:
    """Find pattern matches in a single line using tree-sitter."""
    matches = []
    pattern = rule["pattern"]

    if not HAS_TREE_SITTER:
        return matches

    # Create a pattern matcher
    matcher = PatternMatcher()

    # Match the pattern against the source
    pattern_matches = matcher.match(pattern, language, source)

    # Filter to only matches in this line
    for match in pattern_matches:
        if match["start"]["line"] == line_num + 1:
            matches.append(match)

    return matches


def find_selector_matches(
    file_path: Path,
    rule: dict[str, Any],
    language: str,
    source: bytes
) -> list[dict[str, Any]]:
    """Find matches for a selector rule using tree-sitter."""
    matches = []

    if not HAS_TREE_SITTER:
        return matches

    selector = rule["selector"]

    # Get parser for the language
    if language not in PatternMatcher.LANGUAGE_MAP:
        return matches

    lang_module, _ = PatternMatcher.LANGUAGE_MAP[language]
    parser = ts.Parser()
    parser.language = ts.Language(lang_module.language())

    # Parse the source
    tree = parser.parse(source)

    # Walk the AST and find all nodes matching the selector type
    root_node = tree.root_node

    def walk_node(node: ts.Node):
        nonlocal matches
        # Check if this node matches the selector
        if node.type == selector:
            # Get node text and position
            node_text = source[node.start_byte:node.end_byte].decode("utf-8")
            position = {
                "start": {
                    "line": node.start_point[0] + 1,
                    "col": node.start_point[1] + 1
                },
                "end": {
                    "line": node.end_point[0] + 1,
                    "col": node.end_point[1] + 1
                }
            }
            matches.append({
                "start": position["start"],
                "end": position["end"],
                "match": node_text
                # No captures for selector matches
            })

        # Recurse into children
        for child in node.children:
            walk_node(child)

    walk_node(root_node)
    return matches


def ranges_overlap(start1: dict, end1: dict, start2: dict, end2: dict) -> bool:
    """Check if two ranges overlap."""
    # First compare by file - assume same file for overlap check
    # Convert to comparable tuples
    r1_start = (start1["line"], start1["col"])
    r1_end = (end1["line"], end1["col"])
    r2_start = (start2["line"], start2["col"])
    r2_end = (end2["line"], end2["col"])

    # Ranges overlap if one starts before/at the other ends and vice versa
    return not (r1_end < r2_start or r2_end < r1_start)


def apply_fixes_to_file(file_path: Path, fixes: list[dict[str, Any]]) -> None:
    """Apply a list of non-overlapping fixes to a file."""
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Convert string to list of chars for easy manipulation
    chars = list(content)

    # Sort fixes by start position descending so we can apply from end to start
    sorted_fixes = sorted(fixes, key=lambda f: (-(f["start"]["line"]), -(f["start"]["col"]), f["rule_id"]))

    for fix in sorted_fixes:
        # Convert line/col to byte offset
        lines = content.split('\n')
        offset = 0
        for i in range(fix["start"]["line"] - 1):
            offset += len(lines[i]) + 1  # +1 for newline
        offset += fix["start"]["col"] - 1

        # Apply the replacement
        replacement = fix["replacement"]
        chars = chars[:offset] + list(replacement) + chars[offset + (fix["end"]["col"] - fix["start"]["col"]):]

    # Write back
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(''.join(chars))


def search_directory(
    root_dir: Path,
    rules: list[dict[str, Any]],
    encoding: str,
    args: argparse.Namespace
) -> list[dict[str, Any]]:
    """Search all files in a directory recursively."""
    all_matches = []

    for file_path in root_dir.rglob("*"):
        if file_path.is_file():
            matches = search_file(file_path, root_dir, rules, encoding)
            all_matches.extend(matches)

    # Sort by: file, start.line, start.col, rule_id, then end.position for ties
    all_matches.sort(key=lambda m: (
        m["file"],
        m["start"]["line"],
        m["start"]["col"],
        m["rule_id"],
        m["end"]["line"],
        m["end"]["col"]
    ))

    # If no fix mode, just return matches
    if not args.dry_run and not args.apply_fixes:
        return all_matches

    # Process fixes
    # Group matches by file
    file_matches: dict[str, list[dict[str, Any]]] = {}
    for match in all_matches:
        file_name = match["file"]
        if file_name not in file_matches:
            file_matches[file_name] = []
        file_matches[file_name].append(match)

    # Process each file
    fix_outputs = []  # Output lines for fixes

    for file_name, matches in file_matches.items():
        # Get the actual file path
        file_path = root_dir / file_name

        # Build fix candidates
        fix_candidates = []
        for match in matches:
            rule = next((r for r in rules if r["id"] == match["rule_id"]), None)
            if rule and "fix" in rule:
                template = rule["fix"]["template"]
                captures = match.get("captures", {})
                # Extract capture texts for expansion
                capture_texts = {}
                for cap_name, cap_value in captures.items():
                    if isinstance(cap_value, dict) and "text" in cap_value:
                        capture_texts[cap_name] = cap_value["text"]
                    elif isinstance(cap_value, str):
                        capture_texts[cap_name] = cap_value

                replacement = expand_template(template, match["match"], capture_texts)

                fix_candidates.append({
                    "rule_id": match["rule_id"],
                    "file": file_name,
                    "language": match["language"],
                    "start": match["start"],
                    "end": match["end"],
                    "replacement": replacement,
                    "match": match
                })

        # Sort fixes by position for conflict resolution
        fix_candidates.sort(key=lambda f: (
            f["start"]["line"],
            f["start"]["col"],
            f["rule_id"]
        ))

        # Resolve conflicts
        non_overlapping_fixes = []
        applied_fixes = []

        for candidate in fix_candidates:
            is_overlap = False
            for applied in applied_fixes:
                if ranges_overlap(
                    candidate["start"], candidate["end"],
                    applied["start"], applied["end"]
                ):
                    is_overlap = True
                    break

            if is_overlap:
                # Skip this fix due to overlap
                fix_outputs.append({
                    "event": "fix",
                    "rule_id": candidate["rule_id"],
                    "file": candidate["file"],
                    "language": candidate["language"],
                    "start": candidate["start"],
                    "end": candidate["end"],
                    "replacement": candidate["replacement"],
                    "applied": False,
                    "skipped_reason": "overlap"
                })
            else:
                non_overlapping_fixes.append(candidate)
                applied_fixes.append(candidate)

                if args.apply_fixes:
                    # Apply the fix (will be written later)
                    pass

        # Apply fixes to disk if --apply-fixes
        if args.apply_fixes and non_overlapping_fixes:
            apply_fixes_to_file(file_path, non_overlapping_fixes)

        # Generate fix output lines
        for fix in non_overlapping_fixes:
            fix_outputs.append({
                "event": "fix",
                "rule_id": fix["rule_id"],
                "file": fix["file"],
                "language": fix["language"],
                "start": fix["start"],
                "end": fix["end"],
                "replacement": fix["replacement"],
                "applied": args.apply_fixes,
                "skipped_reason": None
            })

    # Now output all lines (matches and fixes) in proper order
    # Merge matches and fix_outputs
    all_output = []

    for match in all_matches:
        all_output.append({
            "type": "match",
            "data": {
                "rule_id": match["rule_id"],
                "file": match["file"],
                "language": match["language"],
                "start": match["start"],
                "end": match["end"],
                "match": match["match"],
                "captures": match.get("captures")
            }
        })

    for fix in fix_outputs:
        all_output.append({
            "type": "fix",
            "data": fix
        })

    # Sort: file, start.line, start.col, rule_id, then match before fix
    all_output.sort(key=lambda x: (
        x["data"]["file"],
        x["data"]["start"]["line"],
        x["data"]["start"]["col"],
        x["data"]["rule_id"],
        0 if x["type"] == "match" else 1  # match before fix
    ))

    # Output
    for item in all_output:
        output_data = {
            "rule_id": item["data"]["rule_id"],
            "file": item["data"]["file"],
            "language": item["data"]["language"],
            "start": item["data"]["start"],
            "end": item["data"]["end"]
        }

        if item["type"] == "match":
            output_data["match"] = item["data"]["match"]
            if item["data"].get("captures"):
                sorted_captures = dict(sorted(item["data"]["captures"].items()))
                output_data["captures"] = sorted_captures
        else:  # fix
            output_data["replacement"] = item["data"]["replacement"]
            output_data["applied"] = item["data"]["applied"]
            output_data["skipped_reason"] = item["data"]["skipped_reason"]

        print(json.dumps(output_data))

    return all_matches


def main() -> None:
    """Main entry point."""
    args = parse_args()

    root_dir = Path(args.root_dir).resolve()
    if not root_dir.exists():
        print(f"Error: Directory '{args.root_dir}' does not exist", file=sys.stderr)
        sys.exit(1)

    if not root_dir.is_dir():
        print(f"Error: '{args.root_dir}' is not a directory", file=sys.stderr)
        sys.exit(1)

    rules_path = Path(args.rules)
    if not rules_path.exists():
        print(f"Error: Rules file '{args.rules}' does not exist", file=sys.stderr)
        sys.exit(1)

    try:
        rules = load_rules(str(rules_path))
    except (json.JSONDecodeError, ValueError) as e:
        print(f"Error loading rules: {e}", file=sys.stderr)
        sys.exit(1)

    search_directory(root_dir, rules, args.encoding, args)


if __name__ == "__main__":
    main()
