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
import tree_sitter_rust
import tree_sitter_java
import tree_sitter_go
import tree_sitter_haskell
from tree_sitter import Language, Parser, Node


# Language configurations
# Maps language name to (primary_extension, tree_sitter_language_object)
LANGUAGE_MAP = {
    "python": (".py", tree_sitter_python.language()),
    "javascript": (".js", tree_sitter_javascript.language()),
    "cpp": (".cpp", tree_sitter_cpp.language()),
    "rust": (".rs", tree_sitter_rust.language()),
    "java": (".java", tree_sitter_java.language()),
    "go": (".go", tree_sitter_go.language()),
    "haskell": (".hs", tree_sitter_haskell.language()),
}

# Additional extension mappings (language -> list of extensions)
LANGUAGE_EXTENSIONS = {
    "python": [".py"],
    "javascript": [".js", ".mjs", ".cjs"],
    "cpp": [".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx"],
    "rust": [".rs"],
    "java": [".java"],
    "go": [".go"],
    "haskell": [".hs", ".lhs"],
}

# Reverse map from extension to language
EXTENSION_TO_LANG = {}
for lang, extensions in LANGUAGE_EXTENSIONS.items():
    for ext in extensions:
        EXTENSION_TO_LANG[ext] = lang

# Create parsers for each language
PARSERS: Dict[str, Parser] = {}
for lang_name, (_, lang_obj) in LANGUAGE_MAP.items():
    PARSERS[lang_name] = Parser(Language(lang_obj))

# Allowed selector node types
ALLOWED_SELECTORS = {
    "program", "module", "namespace", "import", "export", "declaration", "definition",
    "variable_declaration", "constant_declaration", "type_declaration", "function_declaration",
    "method_declaration", "class_declaration", "interface_declaration", "struct_declaration",
    "enum_declaration", "field_declaration", "type", "generic_type", "parameter", "argument",
    "block", "statement", "return_statement", "throw_statement", "break_statement",
    "continue_statement", "if_statement", "else_clause", "switch_statement", "case_clause",
    "for_statement", "while_statement", "do_while_statement", "try_statement", "catch_clause",
    "finally_clause", "expression", "assignment_expression", "call_expression", "member_expression",
    "subscript_expression", "binary_expression", "unary_expression", "conditional_expression",
    "literal", "string_literal", "numeric_literal", "boolean_literal", "null_literal",
    "array_literal", "object_literal", "annotation", "decorator", "attribute", "access_modifier",
    "operator", "identifier", "comment"
}


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
    fix_group = parser.add_mutually_exclusive_group()
    fix_group.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview fixes without writing to disk"
    )
    fix_group.add_argument(
        "--apply-fixes",
        action="store_true",
        help="Apply fixes and write changes to disk"
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

        rule_id = rule["id"]
        if not isinstance(rule_id, str) or not rule_id:
            raise ValueError(f"Rule {i} 'id' must be a non-empty string")

        if rule_id in seen_ids:
            raise ValueError(f"Duplicate rule id: {rule_id}")
        seen_ids.add(rule_id)

        kind = rule["kind"]
        if kind not in ("exact", "regex", "pattern", "selector"):
            raise ValueError(f"Rule {i} 'kind' must be 'exact', 'regex', 'pattern', or 'selector'")

        # Validate pattern field for non-selector kinds
        pattern = None
        if kind != "selector":
            if "pattern" not in rule:
                raise ValueError(f"Rule {i} missing 'pattern' field")
            pattern = rule["pattern"]
            if not isinstance(pattern, str) or not pattern:
                raise ValueError(f"Rule {i} 'pattern' must be a non-empty string")

        # Validate selector field for selector kind
        selector = None
        if kind == "selector":
            if "selector" not in rule:
                raise ValueError(f"Rule {i} missing 'selector' field")
            selector = rule["selector"]
            if not isinstance(selector, str) or not selector:
                raise ValueError(f"Rule {i} 'selector' must be a non-empty string")
            if selector not in ALLOWED_SELECTORS:
                raise ValueError(f"Rule {i} 'selector' must be an allowed node type")

        # Validate languages if present
        valid_languages = ["python", "javascript", "cpp", "rust", "java", "go", "haskell"]
        languages = rule.get("languages", valid_languages)
        if not isinstance(languages, list):
            raise ValueError(f"Rule {i} 'languages' must be an array")
        for lang in languages:
            if lang not in valid_languages:
                raise ValueError(f"Rule {i} 'languages' may only contain 'python', 'javascript', 'cpp', 'rust', 'java', 'go', 'haskell'")

        # Validate regex_flags if present
        regex_flags = rule.get("regex_flags", [])
        if kind == "regex":
            if not isinstance(regex_flags, list):
                raise ValueError(f"Rule {i} 'regex_flags' must be an array")
            for flag in regex_flags:
                if flag not in ("i", "m", "s"):
                    raise ValueError(f"Rule {i} 'regex_flags' may only contain 'i', 'm', 's'")

        # Validate fix object if present
        fix = None
        if "fix" in rule:
            fix = rule["fix"]
            if not isinstance(fix, dict):
                raise ValueError(f"Rule {i} 'fix' must be an object")
            if "kind" not in fix:
                raise ValueError(f"Rule {i} fix missing 'kind' field")
            if fix["kind"] != "replace":
                raise ValueError(f"Rule {i} fix 'kind' must be 'replace'")
            if "template" not in fix:
                raise ValueError(f"Rule {i} fix missing 'template' field")
            if not isinstance(fix["template"], str):
                raise ValueError(f"Rule {i} fix 'template' must be a string")

        validated_rule = {
            "id": rule_id,
            "kind": kind,
            "languages": languages,
            "regex_flags": regex_flags if kind == "regex" else [],
            "fix": fix
        }

        if pattern is not None:
            validated_rule["pattern"] = pattern
        if selector is not None:
            validated_rule["selector"] = selector

        validated_rules.append(validated_rule)

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
    end_col = node.end_point.column + 1  # Convert to 1-based
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
    # Special handling for Go: type_conversion_expression in pattern can match call_expression in source
    # because Go's grammar is ambiguous for single-argument calls
    types_match = pattern_node.type == source_node.type
    go_special_match = (
        pattern_node.type == 'type_conversion_expression' and
        source_node.type == 'call_expression'
    )
    # Also allow qualified_type to match selector_expression for Go
    go_selector_match = (
        pattern_node.type == 'qualified_type' and
        source_node.type == 'selector_expression'
    )
    # Allow package_identifier to match identifier for Go
    go_pkg_match = (
        pattern_node.type == 'package_identifier' and
        source_node.type == 'identifier'
    )
    # Allow type_identifier to match field_identifier for Go
    go_type_field_match = (
        pattern_node.type == 'type_identifier' and
        source_node.type == 'field_identifier'
    )
    # Allow identifier placeholder to match argument_list for Go
    go_arg_match = (
        pattern_node.type == 'identifier' and
        source_node.type == 'argument_list' and
        pattern_text_full in metavariables
    )
    # Allow variable placeholder to match literal for Haskell
    hs_literal_match = (
        pattern_node.type == 'variable' and
        source_node.type == 'literal' and
        pattern_text_full in metavariables
    )
    # Allow variable placeholder to match token_tree content for Rust macros
    rust_token_match = (
        pattern_node.type == 'identifier' and
        source_node.type in ('string_literal', 'integer_literal', 'literal') and
        pattern_text_full in metavariables
    )

    if not types_match and not go_special_match and not go_selector_match and not go_pkg_match and not go_type_field_match and not go_arg_match and not hs_literal_match and not rust_token_match:
        return False

    # Special case: if pattern is a metavariable matching an argument_list, capture it
    # For Go, we capture the content inside the argument_list (without outer parens)
    if go_arg_match:
        meta = metavariables[pattern_text_full]
        meta_name = meta.base_name

        # Get the content inside the argument_list (skip the opening paren)
        # Find the content between ( and )
        arg_content_start = None
        arg_content_end = None
        for i, child in enumerate(source_node.children):
            if child.type == '(':
                arg_content_start = i + 1
            elif child.type == ')':
                arg_content_end = i
                break

        if arg_content_start is not None and arg_content_end is not None:
            # Get the text of arguments (children between parens)
            arg_children = source_node.children[arg_content_start:arg_content_end]
            if arg_children:
                start_child = arg_children[0]
                end_child = arg_children[-1]
                source_text = content[start_child.start_byte:end_child.end_byte]
                start_line, start_col = start_child.start_point.row + 1, start_child.start_point.column + 1
                end_line, end_col = end_child.end_point.row + 1, end_child.end_point.column + 1
            else:
                # Empty argument list
                source_text = ""
                start_line = source_node.start_point.row + 1
                start_col = source_node.start_point.column + 2  # After (
                end_line = start_line
                end_col = start_col
        else:
            source_text = get_node_text(source_node, source)
            start_line, start_col, end_line, end_col = node_to_line_col(source_node, content)

        # Check consistency
        if meta_name in captures:
            first_pos = captures[meta_name][0]
            first_start_pos = get_position_from_line_col(content, first_pos[0], first_pos[1])
            first_end_pos = get_position_from_line_col(content, first_pos[2], first_pos[3])
            first_text = content[first_start_pos:first_end_pos]
            if source_text != first_text:
                return False

        if meta_name not in captures:
            captures[meta_name] = []
        captures[meta_name].append((start_line, start_col, end_line, end_col))
        return True

    # Handle Haskell variable matching literal
    if hs_literal_match:
        meta = metavariables[pattern_text_full]
        meta_name = meta.base_name
        source_text = get_node_text(source_node, source)

        # Check consistency
        if meta_name in captures:
            first_pos = captures[meta_name][0]
            first_start_pos = get_position_from_line_col(content, first_pos[0], first_pos[1])
            first_end_pos = get_position_from_line_col(content, first_pos[2], first_pos[3])
            first_text = content[first_start_pos:first_end_pos]
            if source_text != first_text:
                return False

        start_line, start_col, end_line, end_col = node_to_line_col(source_node, content)
        if meta_name not in captures:
            captures[meta_name] = []
        captures[meta_name].append((start_line, start_col, end_line, end_col))
        return True

    # Handle Rust metavariable matching literals
    if rust_token_match:
        meta = metavariables[pattern_text_full]
        meta_name = meta.base_name
        source_text = get_node_text(source_node, source)

        # Check consistency
        if meta_name in captures:
            first_pos = captures[meta_name][0]
            first_start_pos = get_position_from_line_col(content, first_pos[0], first_pos[1])
            first_end_pos = get_position_from_line_col(content, first_pos[2], first_pos[3])
            first_text = content[first_start_pos:first_end_pos]
            if source_text != first_text:
                return False

        start_line, start_col, end_line, end_col = node_to_line_col(source_node, content)
        if meta_name not in captures:
            captures[meta_name] = []
        captures[meta_name].append((start_line, start_col, end_line, end_col))
        return True

    # Get named children
    pattern_children = [c for c in pattern_node.children if c.is_named]
    source_children = [c for c in source_node.children if c.is_named]

    # For Go special case: type_conversion_expression matching call_expression
    if go_special_match:
        # Match children: qualified_type with selector_expression, identifier with argument_list
        if len(pattern_children) == 2 and len(source_children) == 2:
            # Match qualified_type (pattern) with selector_expression (source)
            if pattern_children[0].type == 'qualified_type' and source_children[0].type == 'selector_expression':
                if not match_node_with_pattern(pattern_children[0], source_children[0], source, captures, metavariables, pattern_source):
                    return False
            else:
                return False

            # Match identifier placeholder (pattern) with argument_list (source)
            if pattern_children[1].type == 'identifier' and source_children[1].type == 'argument_list':
                if not match_node_with_pattern(pattern_children[1], source_children[1], source, captures, metavariables, pattern_source):
                    return False
            else:
                return False

            return True
        return False

    # For Go special case: qualified_type matching selector_expression
    if go_selector_match:
        # qualified_type has: package_identifier, ., type_identifier
        # selector_expression has: identifier, ., field_identifier
        pattern_sub_children = [c for c in pattern_children if c.is_named]
        source_sub_children = [c for c in source_children if c.is_named]

        if len(pattern_sub_children) != len(source_sub_children):
            return False

        for pc, sc in zip(pattern_sub_children, source_sub_children):
            # package_identifier matches identifier, type_identifier matches field_identifier
            if not match_node_with_pattern(pc, sc, source, captures, metavariables, pattern_source):
                return False
        return True

    # If no named children on both sides, compare text directly
    if not pattern_children and not source_children:
        pattern_text = get_node_text(pattern_node, pattern_source)
        source_text = get_node_text(source_node, source)
        # For Go package_identifier matching identifier, compare text
        if go_pkg_match or go_type_field_match:
            return pattern_text == source_text
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
            container_types = ('translation_unit', 'module', 'program', 'source_file', 'expression_statement')
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


def find_selector_matches(
    content: str,
    selector: str,
    rule_id: str,
    file_path: str,
    language: str
) -> List[Dict[str, Any]]:
    """Find all AST nodes of a specific type."""
    matches = []

    parser = PARSERS.get(language)
    if parser is None:
        return matches

    try:
        source_bytes = content.encode('utf-8')
        tree = parser.parse(source_bytes)
        root = tree.root_node
    except Exception:
        return matches

    def walk_and_collect(node: Node, results: List[Dict[str, Any]]):
        # Check if this node matches the selector
        if node.type == selector:
            start_line, start_col, end_line, end_col = node_to_line_col(node, content)
            match_text = get_node_text(node, source_bytes)

            results.append({
                "rule_id": rule_id,
                "file": file_path,
                "language": language,
                "start": {"line": start_line, "col": start_col},
                "end": {"line": end_line, "col": end_col},
                "match": match_text
            })

        # Recursively check children
        for child in node.children:
            walk_and_collect(child, results)

    walk_and_collect(root, matches)
    return matches


def expand_template(template: str, match_text: str, captures: Optional[Dict[str, Any]] = None) -> str:
    """
    Expand a template string with placeholders.

    Placeholders:
    - $NAME: replaced by the text of the capture's first occurrence
    - $MATCH: replaced by the full matched text
    - $$: literal $
    """
    result = template

    # First, handle $$ -> $
    result = result.replace('$$', '$')

    # Handle $MATCH
    result = result.replace('$MATCH', match_text)

    # Handle capture placeholders
    if captures:
        for cap_name in sorted(captures.keys()):
            cap_text = captures[cap_name]["text"]
            result = result.replace(cap_name, cap_text)

    return result


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
        elif rule["kind"] == "selector":
            matches = find_selector_matches(content, rule["selector"], rule["id"], file_str, language)
        else:  # pattern
            matches = find_pattern_matches(content, rule["pattern"], rule["id"], file_str, language)

        all_matches.extend(matches)

    return all_matches


def sort_key(match: Dict[str, Any]) -> Tuple[str, int, int, str]:
    """
    Create sort key for a match.
    Sort by: file, start.line, start.col, rule_id
    """
    return (
        match["file"],
        match["start"]["line"],
        match["start"]["col"],
        match["rule_id"]
    )


def ranges_overlap(start1: Dict[str, int], end1: Dict[str, int],
                   start2: Dict[str, int], end2: Dict[str, int]) -> bool:
    """Check if two ranges overlap."""
    # Convert to comparable tuples (line, col)
    def pos_to_tuple(p: Dict[str, int]) -> Tuple[int, int]:
        return (p["line"], p["col"])

    s1 = pos_to_tuple(start1)
    e1 = pos_to_tuple(end1)
    s2 = pos_to_tuple(start2)
    e2 = pos_to_tuple(end2)

    # Range 1 starts before range 2 ends AND range 2 starts before range 1 ends
    # But we only check if range 2 starts within range 1 (range 1 was processed first)
    return s1 <= s2 < e1


def apply_fixes_to_file(content: str, fixes: List[Dict[str, Any]]) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Apply fixes to file content, handling overlaps.
    Returns the new content and list of fix result objects.
    """
    # Sort fixes by position (already should be sorted)
    fixes = sorted(fixes, key=lambda f: (f["start"]["line"], f["start"]["col"], f["rule_id"]))

    applied_ranges: List[Tuple[Dict[str, int], Dict[str, int]]] = []
    fix_results = []

    # We need to work with byte positions for proper replacement
    lines = content.split('\n')

    for fix in fixes:
        start = fix["start"]
        end = fix["end"]

        # Check for overlap with already applied fixes
        overlaps = False
        for (applied_start, applied_end) in applied_ranges:
            if ranges_overlap(applied_start, applied_end, start, end):
                overlaps = True
                break

        if overlaps:
            fix_results.append({
                "event": "fix",
                "rule_id": fix["rule_id"],
                "file": fix["file"],
                "language": fix["language"],
                "start": start,
                "end": end,
                "replacement": fix["replacement"],
                "applied": False,
                "skipped_reason": "overlap"
            })
        else:
            fix_results.append({
                "event": "fix",
                "rule_id": fix["rule_id"],
                "file": fix["file"],
                "language": fix["language"],
                "start": start,
                "end": end,
                "replacement": fix["replacement"],
                "applied": True,
                "skipped_reason": None
            })
            applied_ranges.append((start, end))

    # Now apply the fixes in reverse order (from end to beginning)
    # so that positions don't shift
    applied_fixes = [f for f in fix_results if f["applied"]]
    applied_fixes.reverse()

    # Convert line/col positions to character offsets
    line_offsets = [0]
    for line in lines:
        line_offsets.append(line_offsets[-1] + len(line) + 1)  # +1 for newline

    def pos_to_offset(pos: Dict[str, int]) -> int:
        line_idx = pos["line"] - 1  # 0-based
        col_idx = pos["col"] - 1    # 0-based
        return line_offsets[line_idx] + col_idx

    # Build new content with fixes applied
    content_list = list(content)

    for fix in applied_fixes:
        start_offset = pos_to_offset(fix["start"])
        end_offset = pos_to_offset(fix["end"])

        # Replace the range
        content_list[start_offset:end_offset] = list(fix["replacement"])

    new_content = ''.join(content_list)
    return new_content, fix_results


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

    # Build a rule lookup dict
    rule_by_id = {rule["id"]: rule for rule in rules}

    # Process all files and collect matches
    all_matches = []
    for language, files in files_by_lang.items():
        for src_file in files:
            matches = process_file(src_file, root_dir, rules, args.encoding, language)
            all_matches.extend(matches)

    # Sort matches by file, start.line, start.col, rule_id
    all_matches.sort(key=sort_key)

    # If no fix flags, just output matches
    if not args.dry_run and not args.apply_fixes:
        for match in all_matches:
            print(json.dumps(match))
        return 0

    # Build fix objects for matches that have fixes
    fixes_by_file: Dict[str, List[Dict[str, Any]]] = {}
    output_items = []  # List of (match_or_fix, is_fix)

    for match in all_matches:
        rule = rule_by_id.get(match["rule_id"])
        if rule and rule.get("fix"):
            fix_obj = rule["fix"]
            match_text = match["match"]
            captures = match.get("captures")

            replacement = expand_template(
                fix_obj["template"],
                match_text,
                captures
            )

            fix_entry = {
                "rule_id": match["rule_id"],
                "file": match["file"],
                "language": match["language"],
                "start": match["start"],
                "end": match["end"],
                "replacement": replacement
            }

            if match["file"] not in fixes_by_file:
                fixes_by_file[match["file"]] = []
            fixes_by_file[match["file"]].append(fix_entry)

            output_items.append((match, False))  # match before fix
            output_items.append((fix_entry, True))  # fix

    # Apply fixes
    if args.apply_fixes:
        # Read file contents and apply fixes
        for file_path, fixes in fixes_by_file.items():
            full_path = root_dir / file_path
            try:
                with open(full_path, 'r', encoding=args.encoding) as f:
                    content = f.read()
            except (UnicodeDecodeError, IOError):
                continue

            new_content, fix_results = apply_fixes_to_file(content, fixes)

            # Write back
            try:
                with open(full_path, 'w', encoding=args.encoding) as f:
                    f.write(new_content)
            except IOError:
                continue

        # Output in sorted order with proper applied status
        # We need to regenerate the output with proper fix results
        output_items = []
        applied_fix_results: Dict[str, Dict[str, Dict[str, Any]]] = {}  # file -> (rule_id+start -> result)

        for file_path, fixes in fixes_by_file.items():
            full_path = root_dir / file_path
            try:
                with open(full_path, 'r', encoding=args.encoding) as f:
                    content = f.read()
            except (UnicodeDecodeError, IOError):
                continue

            _, fix_results = apply_fixes_to_file(content, fixes)
            for fr in fix_results:
                key = f"{fr['rule_id']}:{fr['start']['line']}:{fr['start']['col']}"
                if file_path not in applied_fix_results:
                    applied_fix_results[file_path] = {}
                applied_fix_results[file_path][key] = fr

        # Build final output
        for match in all_matches:
            rule = rule_by_id.get(match["rule_id"])
            print(json.dumps(match))
            if rule and rule.get("fix"):
                key = f"{match['rule_id']}:{match['start']['line']}:{match['start']['col']}"
                file_path = match["file"]
                if file_path in applied_fix_results and key in applied_fix_results[file_path]:
                    print(json.dumps(applied_fix_results[file_path][key]))
    else:  # dry-run
        # Just output matches and fix previews
        for match in all_matches:
            rule = rule_by_id.get(match["rule_id"])
            print(json.dumps(match))
            if rule and rule.get("fix"):
                fix_obj = rule["fix"]
                match_text = match["match"]
                captures = match.get("captures")

                replacement = expand_template(
                    fix_obj["template"],
                    match_text,
                    captures
                )

                fix_output = {
                    "event": "fix",
                    "rule_id": match["rule_id"],
                    "file": match["file"],
                    "language": match["language"],
                    "start": match["start"],
                    "end": match["end"],
                    "replacement": replacement,
                    "applied": False,
                    "skipped_reason": None
                }
                print(json.dumps(fix_output))

    return 0


if __name__ == "__main__":
    sys.exit(main())
