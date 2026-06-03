#!/usr/bin/env python3
"""
Command-line code searcher for Python, JavaScript, and C++ codebases.
Searches for exact matches, regex patterns, and structure-aware patterns.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional, Set
from dataclasses import dataclass

import tree_sitter_python
import tree_sitter_javascript
import tree_sitter_cpp
from tree_sitter import Language, Parser, Node


# Language configurations
LANGUAGE_MAP = {
    "python": (".py", tree_sitter_python.language()),
    "javascript": (".js", tree_sitter_javascript.language()),
    "cpp": (".cpp", tree_sitter_cpp.language()),
}

# Reverse map from extension to language
EXTENSION_TO_LANG = {
    ext: lang for lang, (ext, _) in LANGUAGE_MAP.items()
}

# Create parsers for each language
PARSERS: Dict[str, Parser] = {}
for lang_name, (_, lang_obj) in LANGUAGE_MAP.items():
    PARSERS[lang_name] = Parser(Language(lang_obj))


@dataclass
class Metavariable:
    """Represents a metavariable in a pattern."""
    name: str  # e.g., "$X" or "$X?"
    base_name: str  # e.g., "$X" (without ?)
    is_optional: bool
    placeholder: str  # the placeholder identifier used in transformed pattern


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Search codebases for patterns"
    )
    parser.add_argument(
        "root_dir",
        help="Path to the codebase to scan"
    )
    parser.add_argument(
        "--rules",
        required=True,
        help="Path to a JSON array of rules"
    )
    parser.add_argument(
        "--encoding",
        default="utf-8",
        help="File encoding (default: utf-8)"
    )
    return parser.parse_args()


def load_rules(rules_path: str) -> List[Dict[str, Any]]:
    """Load and validate rules from JSON file."""
    with open(rules_path, 'r', encoding='utf-8') as f:
        rules = json.load(f)

    if not isinstance(rules, list):
        raise ValueError("Rules file must contain a JSON array")

    seen_ids = set()
    validated_rules = []

    for i, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise ValueError(f"Rule {i} must be an object")

        # Check required fields
        if "id" not in rule:
            raise ValueError(f"Rule {i} missing 'id' field")
        if "kind" not in rule:
            raise ValueError(f"Rule {i} missing 'kind' field")
        if "pattern" not in rule:
            raise ValueError(f"Rule {i} missing 'pattern' field")

        rule_id = rule["id"]
        if not isinstance(rule_id, str) or not rule_id:
            raise ValueError(f"Rule {i} 'id' must be a non-empty string")

        if rule_id in seen_ids:
            raise ValueError(f"Duplicate rule id: {rule_id}")
        seen_ids.add(rule_id)

        kind = rule["kind"]
        if kind not in ("exact", "regex", "pattern"):
            raise ValueError(f"Rule {i} 'kind' must be 'exact', 'regex', or 'pattern'")

        pattern = rule["pattern"]
        if not isinstance(pattern, str) or not pattern:
            raise ValueError(f"Rule {i} 'pattern' must be a non-empty string")

        # Validate languages if present
        valid_languages = ["python", "javascript", "cpp"]
        languages = rule.get("languages", valid_languages)
        if not isinstance(languages, list):
            raise ValueError(f"Rule {i} 'languages' must be an array")
        for lang in languages:
            if lang not in valid_languages:
                raise ValueError(f"Rule {i} 'languages' may only contain 'python', 'javascript', 'cpp'")

        # Validate regex_flags if present
        regex_flags = rule.get("regex_flags", [])
        if kind == "regex":
            if not isinstance(regex_flags, list):
                raise ValueError(f"Rule {i} 'regex_flags' must be an array")
            for flag in regex_flags:
                if flag not in ("i", "m", "s"):
                    raise ValueError(f"Rule {i} 'regex_flags' may only contain 'i', 'm', 's'")

        validated_rules.append({
            "id": rule_id,
            "kind": kind,
            "pattern": pattern,
            "languages": languages,
            "regex_flags": regex_flags if kind == "regex" else []
        })

    return validated_rules


def compile_regex_flags(flag_list: List[str]) -> int:
    """Convert flag list to re module flags."""
    flags = 0
    for flag in flag_list:
        if flag == "i":
            flags |= re.IGNORECASE
        elif flag == "m":
            flags |= re.MULTILINE
        elif flag == "s":
            flags |= re.DOTALL
    return flags


def find_source_files(root_dir: str, languages: List[str]) -> Dict[str, List[Path]]:
    """Find all source files for the given languages."""
    root = Path(root_dir).resolve()
    files_by_lang: Dict[str, List[Path]] = {lang: [] for lang in languages}

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        ext = path.suffix
        if ext in EXTENSION_TO_LANG:
            lang = EXTENSION_TO_LANG[ext]
            if lang in files_by_lang:
                files_by_lang[lang].append(path)

    for lang in files_by_lang:
        files_by_lang[lang].sort()

    return files_by_lang


def get_line_col(content: str, pos: int) -> Tuple[int, int]:
    """
    Convert a position in content to (line, col) 1-based coordinates.
    Line and column are both 1-based.
    """
    line = 1
    col = 1
    for i in range(pos):
        if content[i] == '\n':
            line += 1
            col = 1
        else:
            col += 1
    return line, col


def get_position_from_line_col(content: str, line: int, col: int) -> int:
    """Convert 1-based line/col to 0-based position."""
    current_line = 1
    current_col = 1
    pos = 0
    while pos < len(content):
        if current_line == line and current_col == col:
            return pos
        if content[pos] == '\n':
            current_line += 1
            current_col = 1
        else:
            current_col += 1
        pos += 1
    return pos


def find_exact_matches(content: str, pattern: str, rule_id: str, file_path: str, language: str) -> List[Dict[str, Any]]:
    """Find all exact matches of pattern in content."""
    matches = []
    start = 0
    pattern_len = len(pattern)

    while True:
        pos = content.find(pattern, start)
        if pos == -1:
            break

        start_line, start_col = get_line_col(content, pos)
        end_line, end_col = get_line_col(content, pos + pattern_len)

        matches.append({
            "rule_id": rule_id,
            "file": file_path,
            "language": language,
            "start": {"line": start_line, "col": start_col},
            "end": {"line": end_line, "col": end_col},
            "match": pattern
        })

        start = pos + 1

    return matches


def find_regex_matches(content: str, pattern: str, flags: int, rule_id: str, file_path: str, language: str) -> List[Dict[str, Any]]:
    """Find all regex matches in content."""
    matches = []

    try:
        compiled = re.compile(pattern, flags)
    except re.error:
        return matches

    for match in compiled.finditer(content):
        start_pos = match.start()
        end_pos = match.end()
        matched_text = match.group()

        start_line, start_col = get_line_col(content, start_pos)
        end_line, end_col = get_line_col(content, end_pos)

        matches.append({
            "rule_id": rule_id,
            "file": file_path,
            "language": language,
            "start": {"line": start_line, "col": start_col},
            "end": {"line": end_line, "col": end_col},
            "match": matched_text
        })

    return matches


def parse_metavariables(pattern: str) -> Tuple[Dict[str, Metavariable], str]:
    """
    Parse metavariables from the pattern and return them along with
    a valid code pattern (with placeholder identifiers).

    Metavariables: $NAME or $NAME? (optional)
    Literal $$: becomes $

    Returns:
        - Dict mapping placeholder names to Metavariable objects
        - Transformed pattern with valid identifiers as placeholders
    """
    metavariables = {}
    # Regex to find metavariables: $NAME or $NAME?
    # $$ is literal dollar sign
    meta_pattern = re.compile(r'\$\$|\$([A-Za-z_][A-Za-z0-9_]*)(\?)?')

    counter = 0

    def replace_meta(m: re.Match) -> str:
        nonlocal counter
        if m.group(0) == '$$':
            return '$'

        name = '$' + m.group(1)
        is_optional = m.group(2) == '?'

        # Create a unique placeholder identifier
        placeholder = f"__META_{counter}__"
        counter += 1

        metavariables[placeholder] = Metavariable(
            name=name,
            base_name=name,
            is_optional=is_optional,
            placeholder=placeholder
        )

        return placeholder

    # Convert $$ to $ and extract metavariables
    transformed_pattern = meta_pattern.sub(replace_meta, pattern)

    return metavariables, transformed_pattern


def get_node_text(node: Node, source: bytes) -> str:
    """Get the text content of a node."""
    return source[node.start_byte:node.end_byte].decode('utf-8')


def node_to_line_col(node: Node, content: str) -> Tuple[int, int, int, int]:
    """Convert node positions to line/col coordinates (1-based)."""
    # Use tree-sitter's point information directly
    # Point.row is 0-based line, Point.column is 0-based column
    start_line = node.start_point.row + 1  # Convert to 1-based
    start_col = node.start_point.column + 1  # Convert to 1-based
    end_line = node.end_point.row + 1
    end_col = node.end_point.column + 1
    return start_line, start_col, end_line, end_col


def match_node_with_pattern(
    pattern_node: Node,
    source_node: Node,
    source: bytes,
    captures: Dict[str, List[Tuple[int, int, int, int]]],
    metavariables: Dict[str, Metavariable],
    pattern_source: bytes
) -> bool:
    """
    Recursively match a source AST node against a pattern AST node.
    Returns True if match succeeds, False otherwise.
    Updates captures dict with matched metavariables.
    """
    content = source.decode('utf-8')
    pattern_text_full = get_node_text(pattern_node, pattern_source)

    # Check if pattern node is a metavariable placeholder (leaf node)
    if pattern_node.child_count == 0 and pattern_text_full in metavariables:
        meta = metavariables[pattern_text_full]
        meta_name = meta.base_name
        source_text = get_node_text(source_node, source)

        # Check consistency: if already captured, must match same text
        if meta_name in captures:
            first_pos = captures[meta_name][0]
            first_start_pos = get_position_from_line_col(content, first_pos[0], first_pos[1])
            first_end_pos = get_position_from_line_col(content, first_pos[2], first_pos[3])
            first_text = content[first_start_pos:first_end_pos]

            if source_text != first_text:
                return False

        # Record this capture
        start_line, start_col, end_line, end_col = node_to_line_col(source_node, content)
        if meta_name not in captures:
            captures[meta_name] = []
        captures[meta_name].append((start_line, start_col, end_line, end_col))
        return True

    # Check node type match
    if pattern_node.type != source_node.type:
        return False

    # Get named children
    pattern_children = [c for c in pattern_node.children if c.is_named]
    source_children = [c for c in source_node.children if c.is_named]

    # If no named children on both sides, compare text directly
    if not pattern_children and not source_children:
        pattern_text = get_node_text(pattern_node, pattern_source)
        source_text = get_node_text(source_node, source)
        return pattern_text == source_text

    # Try to match children
    if len(pattern_children) != len(source_children):
        # Special case: if pattern has a single metavariable child, it can match multiple source children
        # But for now, require exact child count match
        return False

    # Match each child pair
    for pc, sc in zip(pattern_children, source_children):
        if not match_node_with_pattern(pc, sc, source, captures, metavariables, pattern_source):
            return False

    return True


def find_pattern_matches(
    content: str,
    pattern: str,
    rule_id: str,
    file_path: str,
    language: str
) -> List[Dict[str, Any]]:
    """Find all structure-aware pattern matches in content."""
    matches = []

    # Parse metavariables from pattern
    metavariables, transformed_pattern = parse_metavariables(pattern)

    # Parse the pattern as code
    parser = PARSERS.get(language)
    if parser is None:
        return matches

    try:
        pattern_bytes = transformed_pattern.encode('utf-8')
        pattern_tree = parser.parse(pattern_bytes)
        pattern_root = pattern_tree.root_node

        # Find the first valid node, potentially looking inside error nodes
        pattern_top = None

        def find_valid_node(node):
            """Recursively find the first valid named node that's not a container type."""
            # Container types we want to skip
            container_types = ('translation_unit', 'module', 'program')
            if not node.has_error and node.is_named and node.type not in container_types:
                return node
            for child in node.children:
                if child.is_named:
                    result = find_valid_node(child)
                    if result:
                        return result
            return None

        pattern_top = find_valid_node(pattern_root)

        # If still not found, try direct children
        if pattern_top is None:
            for child in pattern_root.children:
                if not child.has_error and child.is_named:
                    pattern_top = child
                    break
                # Look inside error nodes
                if child.has_error:
                    for subchild in child.children:
                        if subchild.is_named and not subchild.has_error:
                            pattern_top = subchild
                            break
                    if pattern_top:
                        break

        if pattern_top is None:
            return matches

    except Exception:
        return matches

    # Parse the source file
    try:
        source_bytes = content.encode('utf-8')
        source_tree = parser.parse(source_bytes)
        source_root = source_tree.root_node
    except Exception:
        return matches

    # Walk the source tree and try to match at each node
    def walk_and_match(node: Node, results: List[Dict[str, Any]]):
        captures: Dict[str, List[Tuple[int, int, int, int]]] = {}

        # Try to match pattern with this node
        if match_node_with_pattern(pattern_top, node, source_bytes, captures, metavariables, pattern_bytes):
            # We have a match!
            start_line, start_col, end_line, end_col = node_to_line_col(node, content)
            match_text = get_node_text(node, source_bytes)

            # Build captures output
            captures_output = {}
            for meta_name in sorted(captures.keys()):
                positions = captures[meta_name]
                # Get the matched text (from first occurrence)
                first_pos = positions[0]
                first_start_pos = get_position_from_line_col(content, first_pos[0], first_pos[1])
                first_end_pos = get_position_from_line_col(content, first_pos[2], first_pos[3])
                text = content[first_start_pos:first_end_pos]

                ranges = []
                for pos in positions:
                    ranges.append({
                        "start": {"line": pos[0], "col": pos[1]},
                        "end": {"line": pos[2], "col": pos[3]}
                    })

                captures_output[meta_name] = {
                    "text": text,
                    "ranges": ranges
                }

            results.append({
                "rule_id": rule_id,
                "file": file_path,
                "language": language,
                "start": {"line": start_line, "col": start_col},
                "end": {"line": end_line, "col": end_col},
                "match": match_text,
                "captures": captures_output
            })

        # Recursively check children
        for child in node.children:
            walk_and_match(child, results)

    walk_and_match(source_root, matches)

    return matches


def process_file(
    file_path: Path,
    root_dir: Path,
    rules: List[Dict[str, Any]],
    encoding: str,
    language: str
) -> List[Dict[str, Any]]:
    """Process a single file and return all matches."""
    try:
        with open(file_path, 'r', encoding=encoding) as f:
            content = f.read()
    except (UnicodeDecodeError, IOError):
        return []

    # Get relative path with forward slashes
    rel_path = file_path.relative_to(root_dir)
    file_str = str(rel_path).replace(os.sep, '/')

    all_matches = []

    for rule in rules:
        # Skip rules that don't apply to this language
        if language not in rule["languages"]:
            continue

        if rule["kind"] == "exact":
            matches = find_exact_matches(content, rule["pattern"], rule["id"], file_str, language)
        elif rule["kind"] == "regex":
            flags = compile_regex_flags(rule["regex_flags"])
            matches = find_regex_matches(content, rule["pattern"], flags, rule["id"], file_str, language)
        else:  # pattern
            matches = find_pattern_matches(content, rule["pattern"], rule["id"], file_str, language)

        all_matches.extend(matches)

    return all_matches


def sort_key(match: Dict[str, Any]) -> Tuple[str, int, int, int, int, str]:
    """
    Create sort key for a match.
    Sort by: file, start position, end position, rule_id
    """
    return (
        match["file"],
        match["start"]["line"],
        match["start"]["col"],
        match["end"]["line"],
        match["end"]["col"],
        match["rule_id"]
    )


def main() -> int:
    """Main entry point."""
    args = parse_args()

    # Load rules
    try:
        rules = load_rules(args.rules)
    except (json.JSONDecodeError, ValueError, IOError) as e:
        print(f"Error loading rules: {e}", file=sys.stderr)
        return 1

    # Find source files
    root_dir = Path(args.root_dir).resolve()
    if not root_dir.is_dir():
        print(f"Error: {args.root_dir} is not a directory", file=sys.stderr)
        return 1

    # Get all unique languages from rules
    all_languages = set()
    for rule in rules:
        all_languages.update(rule["languages"])

    files_by_lang = find_source_files(args.root_dir, list(all_languages))

    # Process all files
    all_matches = []
    for language, files in files_by_lang.items():
        for src_file in files:
            matches = process_file(src_file, root_dir, rules, args.encoding, language)
            all_matches.extend(matches)

    # Sort matches
    all_matches.sort(key=sort_key)

    # Output matches as JSON Lines
    for match in all_matches:
        print(json.dumps(match))

    return 0


if __name__ == "__main__":
    sys.exit(main())
