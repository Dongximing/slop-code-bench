#!/usr/bin/env python3
"""
Command-line code searcher for Python, JavaScript, and C++ codebases.
Searches for exact matches, regex patterns, and structure-aware patterns with metavariables.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple, Set

import tree_sitter_python
import tree_sitter_javascript
import tree_sitter_cpp
from tree_sitter import Language, Parser, Node


# Language configuration
LANGUAGE_CONFIG = {
    "python": {
        "extensions": [".py"],
        "tree_sitter_lang": tree_sitter_python,
    },
    "javascript": {
        "extensions": [".js"],
        "tree_sitter_lang": tree_sitter_javascript,
    },
    "cpp": {
        "extensions": [".cpp", ".cxx", ".cc", ".hpp", ".hxx", ".hh", ".h", ".c"],
        "tree_sitter_lang": tree_sitter_cpp,
    },
}

# Cache for parsers
_PARSER_CACHE: Dict[str, Parser] = {}


def get_parser(language: str) -> Parser:
    """Get or create a parser for the given language."""
    if language not in _PARSER_CACHE:
        lang_module = LANGUAGE_CONFIG[language]["tree_sitter_lang"]
        parser = Parser(Language(lang_module.language()))
        _PARSER_CACHE[language] = parser
    return _PARSER_CACHE[language]


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Search codebases for pattern matches"
    )
    parser.add_argument(
        "root_dir",
        help="Path to the codebase to scan"
    )
    parser.add_argument(
        "--rules",
        required=True,
        help="Path to JSON file containing search rules"
    )
    parser.add_argument(
        "--encoding",
        default="utf-8",
        help="File encoding (default: utf-8)"
    )
    return parser.parse_args()


def extract_metavariables(pattern: str) -> Tuple[Set[str], Set[str]]:
    """
    Extract metavariables from a pattern string.
    Returns (required_vars, optional_vars) where optional vars end with '?'.
    Also handles $$ as escaped literal $.
    """
    required = set()
    optional = set()

    # First, handle $$ as escape sequence - temporarily replace it
    # We'll use a placeholder that won't appear in normal patterns
    pattern_processed = pattern.replace("$$", "\x00DOLLAR\x00")

    # Find all $NAME or $NAME? patterns
    # NAME must start with an uppercase letter or underscore, followed by alphanumeric/underscore
    # We need to match both regular and optional (ending with ?)
    pattern_re = re.compile(r'\$([A-Z_][A-Za-z0-9_]*)(\?)?')

    for match in pattern_re.finditer(pattern_processed):
        name = match.group(1)
        is_optional = match.group(2) == '?'
        if is_optional:
            optional.add(f"${name}")
        else:
            required.add(f"${name}")

    return required, optional


def pattern_to_source(pattern: str) -> str:
    """
    Convert a pattern string to valid source code for parsing.
    Replaces metavariables with valid placeholder identifiers.
    """
    # Replace $$ with placeholder first
    result = pattern.replace("$$", "\x00DOLLAR\x00")

    # Replace metavariables with valid Python identifiers
    # $NAME? -> __META_NAME__
    # $NAME -> __META_NAME__
    def replace_meta(m):
        name = m.group(1)
        # If it's optional (has ?), skip the ?
        return f"__META_{name}__"

    result = re.sub(r'\$([A-Z_][A-Za-z0-9_]*)(\?)?', replace_meta, result)

    # Restore escaped $
    result = result.replace("\x00DOLLAR\x00", "$")

    return result


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

        # Validate id
        rule_id = rule.get("id")
        if not rule_id or not isinstance(rule_id, str):
            raise ValueError(f"Rule {i}: 'id' must be a non-empty string")
        if rule_id in seen_ids:
            raise ValueError(f"Rule {i}: duplicate id '{rule_id}'")
        seen_ids.add(rule_id)

        # Validate kind
        kind = rule.get("kind")
        if kind not in ("exact", "regex", "pattern"):
            raise ValueError(f"Rule {i}: 'kind' must be 'exact', 'regex', or 'pattern'")

        # Validate pattern
        pattern = rule.get("pattern")
        if not pattern or not isinstance(pattern, str):
            raise ValueError(f"Rule {i}: 'pattern' must be a non-empty string")

        # Validate languages (optional)
        languages = rule.get("languages")
        if languages is None:
            languages = ["python", "javascript", "cpp"]
        if not isinstance(languages, list):
            raise ValueError(f"Rule {i}: 'languages' must be an array")
        for lang in languages:
            if lang not in LANGUAGE_CONFIG:
                raise ValueError(f"Rule {i}: unsupported language '{lang}'")

        # Validate regex_flags (optional, only for regex)
        regex_flags = rule.get("regex_flags", [])
        if kind == "regex":
            if not isinstance(regex_flags, list):
                raise ValueError(f"Rule {i}: 'regex_flags' must be an array")
            valid_flags = {"i", "m", "s"}
            for flag in regex_flags:
                if flag not in valid_flags:
                    raise ValueError(f"Rule {i}: invalid regex flag '{flag}'")
        elif regex_flags:
            raise ValueError(f"Rule {i}: 'regex_flags' only valid for regex rules")

        # Compile regex pattern if needed
        compiled_pattern = None
        if kind == "regex":
            flags = 0
            for flag in regex_flags:
                if flag == "i":
                    flags |= re.IGNORECASE
                elif flag == "m":
                    flags |= re.MULTILINE
                elif flag == "s":
                    flags |= re.DOTALL

            try:
                compiled_pattern = re.compile(pattern, flags)
            except re.error as e:
                raise ValueError(f"Rule {i}: invalid regex pattern: {e}")

        # Extract metavariables for pattern rules
        required_vars = set()
        optional_vars = set()
        pattern_ast_cache = {}

        if kind == "pattern":
            required_vars, optional_vars = extract_metavariables(pattern)

            # Pre-compile patterns for each language
            for lang in languages:
                try:
                    source = pattern_to_source(pattern)
                    parser = get_parser(lang)
                    tree = parser.parse(bytes(source, "utf-8"))
                    pattern_ast_cache[lang] = {
                        "source": source,
                        "tree": tree,
                        "original_pattern": pattern,
                    }
                except Exception as e:
                    raise ValueError(f"Rule {i}: failed to parse pattern for {lang}: {e}")

        validated_rules.append({
            "id": rule_id,
            "kind": kind,
            "pattern": pattern,
            "languages": languages,
            "regex_flags": regex_flags,
            "compiled_pattern": compiled_pattern,
            "required_vars": required_vars,
            "optional_vars": optional_vars,
            "pattern_ast_cache": pattern_ast_cache,
        })

    return validated_rules


def find_files_for_language(root_dir: str, language: str) -> List[Path]:
    """Find all files for a given language recursively under root_dir."""
    files = []
    root_path = Path(root_dir)
    extensions = LANGUAGE_CONFIG[language]["extensions"]

    for ext in extensions:
        for path in root_path.rglob(f"*{ext}"):
            if path.is_file():
                files.append(path)

    return sorted(files)


def get_line_col(content: str, pos: int) -> Tuple[int, int]:
    """Convert a byte position to (line, col) where col is 1-indexed inclusive.

    For start positions: returns the column of the first character.
    For end positions: returns the column of the last character (inclusive).
    """
    line_num = 1
    line_start = 0

    for i in range(pos):
        if i < len(content) and content[i] == '\n':
            line_num += 1
            line_start = i + 1

    col_num = pos - line_start + 1
    return line_num, col_num


def get_line_col_exclusive_end(content: str, pos: int) -> Tuple[int, int]:
    """Get line and column for an end position, returning exclusive column.

    Tree-sitter's end positions are exclusive (one past the last char).
    This returns the 1-indexed exclusive column.
    """
    line_num = 1
    line_start = 0

    for i in range(pos):
        if i < len(content) and content[i] == '\n':
            line_num += 1
            line_start = i + 1

    # pos is exclusive in 0-indexed, convert to 1-indexed exclusive
    col_num = pos - line_start + 1
    return line_num, col_num


def find_exact_matches(content: str, pattern: str, rule_id: str, file_path: str, language: str) -> List[Dict[str, Any]]:
    """Find all exact matches of pattern in content."""
    matches = []

    start = 0
    while True:
        pos = content.find(pattern, start)
        if pos == -1:
            break

        start_line, start_col = get_line_col(content, pos)
        end_pos = pos + len(pattern)
        end_line, end_col = get_line_col(content, end_pos)

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


def find_regex_matches(content: str, compiled_pattern, rule_id: str, file_path: str, language: str) -> List[Dict[str, Any]]:
    """Find all regex matches in content."""
    matches = []

    for match in compiled_pattern.finditer(content):
        start_pos = match.start()
        end_pos = match.end()
        match_text = match.group(0)

        start_line, start_col = get_line_col(content, start_pos)
        end_line, end_col = get_line_col(content, end_pos)

        matches.append({
            "rule_id": rule_id,
            "file": file_path,
            "language": language,
            "start": {"line": start_line, "col": start_col},
            "end": {"line": end_line, "col": end_col},
            "match": match_text
        })

    return matches


def get_node_text(node: Node, source: bytes) -> str:
    """Get the text content of a node."""
    return source[node.start_byte:node.end_byte].decode('utf-8')


def node_matches_pattern(node: Node, pattern_node: Node,
                         source: bytes, pattern_source: bytes,
                         bindings: Dict[str, List[Tuple[Node, bytes]]]) -> bool:
    """
    Check if a source node matches a pattern node.
    bindings is modified in-place to collect metavariable bindings.
    Returns True if match succeeds.
    """
    # Check if pattern node is a metavariable placeholder
    if pattern_node.child_count == 0:
        text = get_node_text(pattern_node, pattern_source)
        # Check for metavariable placeholder like __META_X__
        meta_match = re.match(r'^__META_([A-Z_][A-Za-z0-9_]*)__$', text)
        if meta_match:
            meta_name = f"${meta_match.group(1)}"
            # This is a metavariable - bind it
            if meta_name not in bindings:
                bindings[meta_name] = []
            bindings[meta_name].append((node, source))
            return True

    # Node types must match
    if node.type != pattern_node.type:
        return False

    # For named nodes with children, check children match
    if pattern_node.child_count > 0:
        # Handle optional metavariables - need to match children with optional placeholders
        pattern_children = list(pattern_node.children)
        source_children = list(node.children)

        # Filter out non-meaningful children for comparison
        # We need to handle optional metavariables which might not have corresponding source nodes
        return match_children_with_metavars(source_children, pattern_children,
                                            source, pattern_source, bindings)

    # For leaf nodes, text must match exactly (except for metavariables already handled)
    return get_node_text(node, source) == get_node_text(pattern_node, pattern_source)


def match_children_with_metavars(source_children: List[Node], pattern_children: List[Node],
                                  source: bytes, pattern_source: bytes,
                                  bindings: Dict[str, List[Tuple[Node, bytes]]]) -> bool:
    """
    Match source children against pattern children, handling metavariables.
    """
    # Strategy: try to match each pattern child in order with source children
    # Metavariables can match single tokens/expressions

    p_idx = 0
    s_idx = 0

    while p_idx < len(pattern_children) and s_idx < len(source_children):
        p_child = pattern_children[p_idx]
        s_child = source_children[s_idx]

        # Check if pattern child is a metavariable
        is_meta = False
        if p_child.child_count == 0:
            text = get_node_text(p_child, pattern_source)
            if re.match(r'^__META_[A-Z_][A-Za-z0-9_]*__$', text):
                is_meta = True

        if is_meta:
            # This is a metavariable - bind to the current source child
            text = get_node_text(p_child, pattern_source)
            meta_name = f"${re.match(r'^__META_([A-Z_][A-Za-z0-9_]*)__$', text).group(1)}"

            if meta_name not in bindings:
                bindings[meta_name] = []
            bindings[meta_name].append((s_child, source))

            p_idx += 1
            s_idx += 1
        else:
            # Try to match the nodes
            if p_child.type == s_child.type:
                new_bindings = {}
                if node_matches_pattern_recursive(s_child, p_child, source, pattern_source, new_bindings):
                    bindings.update(new_bindings)
                    p_idx += 1
                    s_idx += 1
                else:
                    return False
            else:
                return False

    # All pattern children should be consumed
    return p_idx == len(pattern_children)


def node_matches_pattern_recursive(node: Node, pattern_node: Node,
                                    source: bytes, pattern_source: bytes,
                                    bindings: Dict[str, List[Tuple[Node, bytes]]]) -> bool:
    """
    Recursively check if a source node matches a pattern node.
    """
    # Check if pattern node is a metavariable placeholder
    if pattern_node.child_count == 0:
        text = get_node_text(pattern_node, pattern_source)
        meta_match = re.match(r'^__META_([A-Z_][A-Za-z0-9_]*)__$', text)
        if meta_match:
            meta_name = f"${meta_match.group(1)}"
            if meta_name not in bindings:
                bindings[meta_name] = []
            bindings[meta_name].append((node, source))
            return True

    # Node types must match
    if node.type != pattern_node.type:
        return False

    # Check children
    if pattern_node.child_count > 0:
        pattern_children = list(pattern_node.children)
        source_children = list(node.children)

        # Filter out trailing punctuation/terminators from source if pattern doesn't have them
        # This handles cases like semicolons in JavaScript
        if len(source_children) > len(pattern_children):
            # Check if trailing source children are punctuation (semicolon, etc.)
            filtered_source_children = []
            for child in source_children:
                if child.type in (';', ',', '(', ')', '{', '}', '[', ']'):
                    continue
                filtered_source_children.append(child)

            # If filtering punctuation helps, use that
            if len(filtered_source_children) == len(pattern_children):
                source_children = filtered_source_children

        if len(pattern_children) != len(source_children):
            return False

        for p_child, s_child in zip(pattern_children, source_children):
            if not node_matches_pattern_recursive(s_child, p_child, source, pattern_source, bindings):
                return False

        return True

    # For leaf nodes, text must match exactly
    return get_node_text(node, source) == get_node_text(pattern_node, pattern_source)


def find_pattern_matches(content: str, rule: Dict, file_path: str, language: str) -> List[Dict[str, Any]]:
    """Find all pattern matches using tree-sitter AST matching."""
    matches = []

    if language not in rule["pattern_ast_cache"]:
        return matches

    pattern_cache = rule["pattern_ast_cache"][language]
    pattern_tree = pattern_cache["tree"]
    pattern_source = pattern_cache["source"]

    parser = get_parser(language)
    source_bytes = bytes(content, "utf-8")
    tree = parser.parse(source_bytes)

    root = tree.root_node
    pattern_root = pattern_tree.root_node

    # Get the actual pattern content node - skip the module wrapper
    # pattern_root is (module (expression_statement ...)) or similar
    # We want to match the content inside the module
    # Also unwrap expression_statement to get to the actual statement
    pattern_content_nodes = []
    for child in pattern_root.children:
        # Unwrap expression_statement if it's the only child
        if child.type == 'expression_statement' and child.child_count == 1:
            pattern_content_nodes.append(child.children[0])
        else:
            pattern_content_nodes.append(child)

    # Walk all nodes in the source tree and try to match
    def walk_and_match(node: Node):
        # Try matching against each pattern content node
        for pattern_content in pattern_content_nodes:
            bindings = {}
            if node_matches_pattern_recursive(node, pattern_content, source_bytes,
                                               bytes(pattern_source, "utf-8"), bindings):
                # Found a match!
                # Validate that repeated metavariables have same text
                valid = True
                final_bindings = {}

                for meta_name, node_list in bindings.items():
                    texts = [get_node_text(n, src) for n, src in node_list]
                    if len(set(texts)) > 1:
                        valid = False
                        break
                    final_bindings[meta_name] = texts[0]

                if valid:
                    # Create match output
                    match_text = get_node_text(node, source_bytes)

                    # Build captures dict with sorted keys
                    captures = {}
                    for meta_name in sorted(final_bindings.keys()):
                        text = final_bindings[meta_name]
                        ranges = []
                        for n, src in bindings[meta_name]:
                            start_line, start_col = get_line_col(content, n.start_byte)
                            end_line, end_col = get_line_col(content, n.end_byte)
                            ranges.append({
                                "start": {"line": start_line, "col": start_col},
                                "end": {"line": end_line, "col": end_col}
                            })
                        captures[meta_name] = {
                            "text": text,
                            "ranges": ranges
                        }

                    start_line, start_col = get_line_col(content, node.start_byte)
                    end_line, end_col = get_line_col_exclusive_end(content, node.end_byte)

                    match_obj = {
                        "rule_id": rule["id"],
                        "file": file_path,
                        "language": language,
                        "start": {"line": start_line, "col": start_col},
                        "end": {"line": end_line, "col": end_col},
                        "match": match_text,
                    }

                    if captures:
                        match_obj["captures"] = captures

                    matches.append(match_obj)
                    break  # Found a match, no need to try other patterns

        # Recurse into children
        for child in node.children:
            walk_and_match(child)

    walk_and_match(root)

    return matches


def process_file(file_path: Path, root_dir: Path, rules: List[Dict],
                 encoding: str, language: str) -> List[Dict[str, Any]]:
    """Process a single file and return all matches."""
    try:
        with open(file_path, 'r', encoding=encoding) as f:
            content = f.read()
    except (UnicodeDecodeError, IOError):
        return []

    rel_path = file_path.relative_to(root_dir).as_posix()
    all_matches = []

    for rule in rules:
        # Check if this rule applies to this language
        if language not in rule["languages"]:
            continue

        if rule["kind"] == "exact":
            matches = find_exact_matches(content, rule["pattern"], rule["id"], rel_path, language)
        elif rule["kind"] == "regex":
            matches = find_regex_matches(content, rule["compiled_pattern"], rule["id"], rel_path, language)
        else:  # pattern
            matches = find_pattern_matches(content, rule, rel_path, language)

        all_matches.extend(matches)

    return all_matches


def sort_key(match: Dict[str, Any]) -> tuple:
    """Generate sort key for a match - file, start pos, end pos, rule_id."""
    return (
        match["file"],
        match["start"]["line"],
        match["start"]["col"],
        match["end"]["line"],
        match["end"]["col"],
        match["rule_id"]
    )


def main():
    """Main entry point."""
    args = parse_args()

    # Load rules
    try:
        rules = load_rules(args.rules)
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as e:
        print(f"Error loading rules: {e}", file=sys.stderr)
        sys.exit(1)

    # Find all files across all languages
    root_path = Path(args.root_dir)
    if not root_path.is_dir():
        print(f"Error: {args.root_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    # Collect all files with their languages
    all_file_langs = []
    for language in LANGUAGE_CONFIG:
        files = find_files_for_language(args.root_dir, language)
        for f in files:
            all_file_langs.append((f, language))

    # Process all files
    all_matches = []
    for file_path, language in all_file_langs:
        matches = process_file(file_path, root_path, rules, args.encoding, language)
        all_matches.extend(matches)

    # Sort matches: file, start position, end position, rule_id
    all_matches.sort(key=sort_key)

    # Output JSON Lines
    for match in all_matches:
        print(json.dumps(match, separators=(',', ':')))

    sys.exit(0)


if __name__ == "__main__":
    main()
