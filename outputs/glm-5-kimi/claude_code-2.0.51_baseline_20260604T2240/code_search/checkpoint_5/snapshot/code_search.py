#!/usr/bin/env python3
"""
Command-line code searcher for Python, JavaScript, and C++ codebases.
Searches for exact matches, regex patterns, and structure-aware patterns with metavariables.
Supports auto-fixes with --dry-run and --apply-fixes modes.
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
import tree_sitter_rust
import tree_sitter_java
import tree_sitter_go
import tree_sitter_haskell
from tree_sitter import Language, Parser, Node


# Language configuration
LANGUAGE_CONFIG = {
    "python": {
        "extensions": [".py"],
        "tree_sitter_lang": tree_sitter_python,
        "pattern_wrapper": None,  # Python parses expressions fine at top level
    },
    "javascript": {
        "extensions": [".js", ".mjs", ".cjs"],
        "tree_sitter_lang": tree_sitter_javascript,
        "pattern_wrapper": None,  # JavaScript parses expressions fine at top level
    },
    "cpp": {
        "extensions": [".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx"],
        "tree_sitter_lang": tree_sitter_cpp,
        "pattern_wrapper": None,  # C++ parses expressions fine at top level
    },
    "rust": {
        "extensions": [".rs"],
        "tree_sitter_lang": tree_sitter_rust,
        "pattern_wrapper": "fn _() { {PATTERN} }",  # Wrap in function for proper expression parsing
    },
    "java": {
        "extensions": [".java"],
        "tree_sitter_lang": tree_sitter_java,
        "pattern_wrapper": "class _ {{ void _() {{ {PATTERN} }} }}",  # Wrap in method
    },
    "go": {
        "extensions": [".go"],
        "tree_sitter_lang": tree_sitter_go,
        "pattern_wrapper": "func _() { {PATTERN} }",  # Wrap in function (single braces)
    },
    "haskell": {
        "extensions": [".hs", ".lhs"],
        "tree_sitter_lang": tree_sitter_haskell,
        "pattern_wrapper": None,  # Haskell may need special handling
    },
}

# Allowed selector node types
ALLOWED_SELECTORS = {
    "program", "module", "namespace", "import", "export", "declaration", "definition",
    "variable_declaration", "constant_declaration", "type_declaration",
    "function_declaration", "method_declaration", "class_declaration",
    "interface_declaration", "struct_declaration", "enum_declaration",
    "field_declaration", "type", "generic_type", "parameter", "argument",
    "block", "statement", "return_statement", "throw_statement", "break_statement",
    "continue_statement", "if_statement", "else_clause", "switch_statement",
    "case_clause", "for_statement", "while_statement", "do_while_statement",
    "try_statement", "catch_clause", "finally_clause", "expression",
    "assignment_expression", "call_expression", "member_expression",
    "subscript_expression", "binary_expression", "unary_expression",
    "conditional_expression", "literal", "string_literal", "numeric_literal",
    "boolean_literal", "null_literal", "array_literal", "object_literal",
    "annotation", "decorator", "attribute", "access_modifier", "operator",
    "identifier", "comment"
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
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview fixes without writing changes to disk"
    )
    group.add_argument(
        "--apply-fixes",
        action="store_true",
        help="Apply fixes and write changes to disk"
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


def validate_fix(rule_idx: int, fix_obj: Any) -> Dict[str, Any]:
    """Validate and return a fix object."""
    if not isinstance(fix_obj, dict):
        raise ValueError(f"Rule {rule_idx}: 'fix' must be an object")

    kind = fix_obj.get("kind")
    if kind != "replace":
        raise ValueError(f"Rule {rule_idx}: 'fix.kind' must be 'replace'")

    template = fix_obj.get("template")
    if template is None:
        raise ValueError(f"Rule {rule_idx}: 'fix.template' is required")
    if not isinstance(template, str):
        raise ValueError(f"Rule {rule_idx}: 'fix.template' must be a string")

    return {
        "kind": "replace",
        "template": template
    }


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
        if kind not in ("exact", "regex", "pattern", "selector"):
            raise ValueError(f"Rule {i}: 'kind' must be 'exact', 'regex', 'pattern', or 'selector'")

        # Validate pattern (required for exact, regex, pattern)
        pattern = rule.get("pattern")
        if kind in ("exact", "regex", "pattern"):
            if not pattern or not isinstance(pattern, str):
                raise ValueError(f"Rule {i}: 'pattern' must be a non-empty string")
        elif kind == "selector":
            selector = rule.get("selector")
            if not selector or not isinstance(selector, str):
                raise ValueError(f"Rule {i}: 'selector' must be a non-empty string")
            if selector not in ALLOWED_SELECTORS:
                raise ValueError(f"Rule {i}: invalid selector '{selector}'")

        # Validate languages (optional)
        languages = rule.get("languages")
        if languages is None:
            languages = list(LANGUAGE_CONFIG.keys())
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
                    wrapper = LANGUAGE_CONFIG[lang].get("pattern_wrapper")

                    if wrapper:
                        # Wrap the pattern for languages that need context
                        wrapped_source = wrapper.replace("{PATTERN}", source)
                        parser = get_parser(lang)
                        tree = parser.parse(bytes(wrapped_source, "utf-8"))
                        pattern_ast_cache[lang] = {
                            "source": source,
                            "wrapped_source": wrapped_source,
                            "tree": tree,
                            "original_pattern": pattern,
                            "wrapped": True,
                        }
                    else:
                        parser = get_parser(lang)
                        tree = parser.parse(bytes(source, "utf-8"))
                        pattern_ast_cache[lang] = {
                            "source": source,
                            "tree": tree,
                            "original_pattern": pattern,
                            "wrapped": False,
                        }
                except Exception as e:
                    raise ValueError(f"Rule {i}: failed to parse pattern for {lang}: {e}")

        # Validate fix (optional)
        fix = None
        if "fix" in rule:
            fix = validate_fix(i, rule["fix"])

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
            "fix": fix,
        })

        # Add selector for selector rules
        if kind == "selector":
            validated_rules[-1]["selector"] = rule.get("selector")

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


def get_line_col_from_exclusive_end(content: str, exclusive_pos: int) -> Tuple[int, int]:
    """Get line and column for an exclusive end position, returning inclusive column.

    Tree-sitter's end positions are exclusive (one past the last char).
    This returns the 1-indexed inclusive column of the last character.
    """
    if exclusive_pos == 0:
        return 1, 1

    # Convert exclusive to inclusive by subtracting 1
    inclusive_pos = exclusive_pos - 1

    line_num = 1
    line_start = 0

    for i in range(inclusive_pos):
        if i < len(content) and content[i] == '\n':
            line_num += 1
            line_start = i + 1

    col_num = inclusive_pos - line_start + 1
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
        end_line, end_col = get_line_col_from_exclusive_end(content, end_pos)

        matches.append({
            "rule_id": rule_id,
            "file": file_path,
            "language": language,
            "start": {"line": start_line, "col": start_col},
            "end": {"line": end_line, "col": end_col},
            "match": pattern,
            "match_start_byte": pos,
            "match_end_byte": end_pos
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
        end_line, end_col = get_line_col_from_exclusive_end(content, end_pos)

        matches.append({
            "rule_id": rule_id,
            "file": file_path,
            "language": language,
            "start": {"line": start_line, "col": start_col},
            "end": {"line": end_line, "col": end_col},
            "match": match_text,
            "match_start_byte": start_pos,
            "match_end_byte": end_pos
        })

    return matches


def get_node_text(node: Node, source: bytes) -> str:
    """Get the text content of a node."""
    return source[node.start_byte:node.end_byte].decode('utf-8')


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

        # Filter out punctuation children that should be ignored for matching
        # (but keep them for structure)
        def is_punctuation(child):
            return child.type in (';', ',', '(', ')', '{', '}', '[', ']', ':')

        # Filter punctuation from both sides for comparison
        pattern_children_filtered = [c for c in pattern_children if not is_punctuation(c)]
        source_children_filtered = [c for c in source_children if not is_punctuation(c)]

        # Special case: if pattern has only one child (a metavariable) and source has multiple,
        # the metavariable can match all of them (for argument lists, etc.)
        if len(pattern_children_filtered) == 1 and len(source_children_filtered) >= 1:
            p_child = pattern_children_filtered[0]
            # Check if p_child is a metavariable
            if p_child.child_count == 0:
                p_text = get_node_text(p_child, pattern_source)
                meta_match = re.match(r'^__META_([A-Z_][A-Za-z0-9_]*)__$', p_text)
                if meta_match:
                    meta_name = f"${meta_match.group(1)}"
                    if meta_name not in bindings:
                        bindings[meta_name] = []
                    # Match all source children to this metavariable
                    for s_child in source_children_filtered:
                        bindings[meta_name].append((s_child, source))
                    return True

        if len(pattern_children_filtered) != len(source_children_filtered):
            return False

        for p_child, s_child in zip(pattern_children_filtered, source_children_filtered):
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
    is_wrapped = pattern_cache.get("wrapped", False)

    # For wrapped patterns, use the wrapped source for byte comparisons
    if is_wrapped:
        wrapped_source = pattern_cache.get("wrapped_source", pattern_source)
        pattern_bytes = bytes(wrapped_source, "utf-8")
    else:
        pattern_bytes = bytes(pattern_source, "utf-8")

    parser = get_parser(language)
    source_bytes = bytes(content, "utf-8")
    tree = parser.parse(source_bytes)

    root = tree.root_node
    pattern_root = pattern_tree.root_node

    # Get the actual pattern content node - skip the module wrapper
    # For wrapped patterns, we need to extract from within the wrapper structure
    pattern_content_nodes = []

    if is_wrapped:
        # Navigate through the wrapper to find the actual pattern
        # Structure varies by language, but we look for the pattern inside blocks/statements
        def extract_pattern_from_wrapper(node: Node, depth: int = 0) -> List[Node]:
            """Extract pattern nodes from within wrapper structures."""
            result = []
            # Look for expression_statement, statement, or similar containers
            if node.type in ('expression_statement', 'statement'):
                if node.child_count > 0:
                    # Get the actual expression/statement inside
                    result.append(node.children[0])
            elif node.type in ('block', 'statement_list', 'function_body', 'method_body', 'compound_statement', 'block_statement', 'source_file'):
                for child in node.children:
                    result.extend(extract_pattern_from_wrapper(child, depth + 1))
            elif node.type in ('function_declaration', 'method_declaration', 'class_declaration', 'function_item'):
                for child in node.children:
                    result.extend(extract_pattern_from_wrapper(child, depth + 1))
            elif depth > 0 and node.type not in ('{', '}', 'func', 'class', 'void', 'identifier', 'parameter_list', '(', ')', 'fn', '_'):
                # This might be the actual pattern content at some depth
                if node.child_count == 0 or node.type in ('call_expression', 'selector_expression',
                                                           'macro_invocation', 'expression_list',
                                                           'function_call', 'application_expression'):
                    result.append(node)
            return result

        pattern_content_nodes = extract_pattern_from_wrapper(pattern_root)
    else:
        # Standard unwrapping for non-wrapped patterns
        for child in pattern_root.children:
            # Unwrap expression_statement if it's the only child
            if child.type == 'expression_statement' and child.child_count == 1:
                pattern_content_nodes.append(child.children[0])
            else:
                pattern_content_nodes.append(child)

    # Walk all nodes in the source tree and try to match
    def walk_and_match(node):
        # Try matching against each pattern content node
        for pattern_content in pattern_content_nodes:
            bindings = {}
            if node_matches_pattern_recursive(node, pattern_content, source_bytes,
                                               pattern_bytes, bindings):
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
                            # End is inclusive for captures
                            end_line, end_col = get_line_col_from_exclusive_end(content, n.end_byte)
                            ranges.append({
                                "start": {"line": start_line, "col": start_col},
                                "end": {"line": end_line, "col": end_col}
                            })
                        captures[meta_name] = {
                            "text": text,
                            "ranges": ranges
                        }

                    start_line, start_col = get_line_col(content, node.start_byte)
                    # End position is inclusive - tree-sitter's end_byte is exclusive
                    end_line, end_col = get_line_col_from_exclusive_end(content, node.end_byte)

                    match_obj = {
                        "rule_id": rule["id"],
                        "file": file_path,
                        "language": language,
                        "start": {"line": start_line, "col": start_col},
                        "end": {"line": end_line, "col": end_col},
                        "match": match_text,
                        "match_start_byte": node.start_byte,
                        "match_end_byte": node.end_byte
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


def find_selector_matches(content: str, rule: Dict, file_path: str, language: str) -> List[Dict[str, Any]]:
    """Find all AST nodes matching the selector type."""
    matches = []
    selector = rule["selector"]

    parser = get_parser(language)
    source_bytes = bytes(content, "utf-8")
    tree = parser.parse(source_bytes)

    root = tree.root_node

    def walk_and_match(node: Node):
        # Check if this node matches the selector
        if node.type == selector:
            match_text = get_node_text(node, source_bytes)
            start_line, start_col = get_line_col(content, node.start_byte)
            # End position is inclusive
            end_line, end_col = get_line_col_from_exclusive_end(content, node.end_byte)

            matches.append({
                "rule_id": rule["id"],
                "file": file_path,
                "language": language,
                "start": {"line": start_line, "col": start_col},
                "end": {"line": end_line, "col": end_col},
                "match": match_text,
                "match_start_byte": node.start_byte,
                "match_end_byte": node.end_byte
            })

        # Recurse into children
        for child in node.children:
            walk_and_match(child)

    walk_and_match(root)

    return matches


def expand_template(template: str, match: Dict[str, Any]) -> str:
    """Expand a template string by replacing placeholders.

    $NAME is replaced by the capture's text (first occurrence).
    $MATCH is replaced by the full matched text.
    $$ is replaced by a literal $.
    """
    # First, replace $$ with a placeholder
    result = template.replace("$$", "\x00DOLLAR\x00")

    # Replace $MATCH
    result = result.replace("$MATCH", match["match"])

    # Replace captures
    if "captures" in match:
        # Sort captures by name to ensure consistent expansion
        for name in sorted(match["captures"].keys()):
            text = match["captures"][name]["text"]
            result = result.replace(name, text)

    # Restore escaped $
    result = result.replace("\x00DOLLAR\x00", "$")

    return result


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
        elif rule["kind"] == "selector":
            matches = find_selector_matches(content, rule, rel_path, language)
        else:  # pattern
            matches = find_pattern_matches(content, rule, rel_path, language)

        # Add fix info to matches if rule has a fix
        if rule.get("fix"):
            for match in matches:
                match["fix"] = rule["fix"]

        all_matches.extend(matches)

    return all_matches


def ranges_overlap(start1_byte: int, end1_byte: int, start2_byte: int, end2_byte: int) -> bool:
    """Check if two byte ranges overlap."""
    return not (end1_byte <= start2_byte or end2_byte <= start1_byte)


def apply_fixes_to_file(file_path: Path, fixes: List[Dict[str, Any]], encoding: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Apply fixes to a file, handling conflicts.

    Returns (new_content, fix_results) where fix_results contains applied/skipped status.
    """
    try:
        with open(file_path, 'r', encoding=encoding) as f:
            content = f.read()
    except (UnicodeDecodeError, IOError):
        return content, []

    # Sort fixes by position (start_byte), then rule_id for stable ordering
    sorted_fixes = sorted(fixes, key=lambda x: (x["match_start_byte"], x["match_end_byte"], x["rule_id"]))

    # Track applied ranges for overlap detection
    applied_ranges = []  # List of (start_byte, end_byte)
    fix_results = []

    # Collect all fixes to apply (after conflict resolution)
    replacements = []  # List of (start_byte, end_byte, replacement_text, fix_info)

    for fix_info in sorted_fixes:
        start_byte = fix_info["match_start_byte"]
        end_byte = fix_info["match_end_byte"]

        # Check for overlap with already applied fixes
        has_overlap = False
        for applied_start, applied_end in applied_ranges:
            if ranges_overlap(start_byte, end_byte, applied_start, applied_end):
                has_overlap = True
                break

        if has_overlap:
            # Skipped due to overlap
            fix_results.append({
                "event": "fix",
                "rule_id": fix_info["rule_id"],
                "file": fix_info["file"],
                "language": fix_info["language"],
                "start": fix_info["start"],
                "end": fix_info["end"],
                "replacement": fix_info["replacement"],
                "applied": False,
                "skipped_reason": "overlap"
            })
        else:
            # Can apply this fix
            replacements.append((start_byte, end_byte, fix_info["replacement"], fix_info))
            applied_ranges.append((start_byte, end_byte))
            fix_results.append({
                "event": "fix",
                "rule_id": fix_info["rule_id"],
                "file": fix_info["file"],
                "language": fix_info["language"],
                "start": fix_info["start"],
                "end": fix_info["end"],
                "replacement": fix_info["replacement"],
                "applied": True,
                "skipped_reason": None
            })

    # Apply replacements from end to start to preserve byte positions
    replacements.sort(key=lambda x: x[0], reverse=True)

    content_bytes = bytearray(content.encode('utf-8'))
    for start_byte, end_byte, replacement, _ in replacements:
        content_bytes[start_byte:end_byte] = replacement.encode('utf-8')

    new_content = content_bytes.decode('utf-8')

    return new_content, fix_results


def sort_key_output(item: Dict[str, Any]) -> tuple:
    """Generate sort key for output items (matches and fixes).

    Sort by: file, start.line, start.col, rule_id
    If tied, match comes before fix.
    """
    is_fix = "event" in item and item["event"] == "fix"
    return (
        item["file"],
        item["start"]["line"],
        item["start"]["col"],
        item["rule_id"],
        1 if is_fix else 0  # match (0) before fix (1)
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
    # But we use start.line, start.col for output ordering
    all_matches.sort(key=lambda m: (m["file"], m["start"]["line"], m["start"]["col"], m["rule_id"]))

    # Determine output mode
    apply_mode = args.apply_fixes
    dry_run_mode = args.dry_run

    # If neither --dry-run nor --apply-fixes, just output matches
    if not apply_mode and not dry_run_mode:
        for match in all_matches:
            # Remove internal fields before output
            output = {k: v for k, v in match.items() if not k.startswith("match_") and k != "fix"}
            print(json.dumps(output, separators=(',', ':')))
        sys.exit(0)

    # Build output items (matches and fixes)
    output_items = []

    # Group matches by file for fix application
    matches_by_file: Dict[str, List[Dict]] = {}
    for match in all_matches:
        file_path = match["file"]
        if file_path not in matches_by_file:
            matches_by_file[file_path] = []
        matches_by_file[file_path].append(match)

    # Process fixes for each file
    fix_results_by_file: Dict[str, List[Dict]] = {}

    if apply_mode:
        # Actually apply fixes to files
        for file_rel_path, file_matches in matches_by_file.items():
            # Collect fixes for this file
            fixes_to_apply = []
            for match in file_matches:
                if "fix" in match and match["fix"]:
                    replacement = expand_template(match["fix"]["template"], match)
                    fixes_to_apply.append({
                        **match,
                        "replacement": replacement
                    })

            if fixes_to_apply:
                # Sort by position
                fixes_to_apply.sort(key=lambda x: (x["match_start_byte"], x["match_end_byte"], x["rule_id"]))

                file_path = root_path / file_rel_path
                new_content, fix_results = apply_fixes_to_file(file_path, fixes_to_apply, args.encoding)

                # Write the file
                with open(file_path, 'w', encoding=args.encoding) as f:
                    f.write(new_content)

                fix_results_by_file[file_rel_path] = fix_results
    else:
        # Dry run - just generate fix previews
        for file_rel_path, file_matches in matches_by_file.items():
            fixes_to_apply = []
            for match in file_matches:
                if "fix" in match and match["fix"]:
                    replacement = expand_template(match["fix"]["template"], match)
                    fixes_to_apply.append({
                        **match,
                        "replacement": replacement
                    })

            if fixes_to_apply:
                # Sort by position
                fixes_to_apply.sort(key=lambda x: (x["match_start_byte"], x["match_end_byte"], x["rule_id"]))

                # For dry-run, all fixes are "previewed" (not applied)
                # We still need to check for overlaps for correct skipped_reason
                applied_ranges = []
                fix_results = []

                for fix_info in fixes_to_apply:
                    start_byte = fix_info["match_start_byte"]
                    end_byte = fix_info["match_end_byte"]

                    # Check for overlap
                    has_overlap = False
                    for applied_start, applied_end in applied_ranges:
                        if ranges_overlap(start_byte, end_byte, applied_start, applied_end):
                            has_overlap = True
                            break

                    if has_overlap:
                        fix_results.append({
                            "event": "fix",
                            "rule_id": fix_info["rule_id"],
                            "file": fix_info["file"],
                            "language": fix_info["language"],
                            "start": fix_info["start"],
                            "end": fix_info["end"],
                            "replacement": fix_info["replacement"],
                            "applied": False,
                            "skipped_reason": "overlap"
                        })
                    else:
                        fix_results.append({
                            "event": "fix",
                            "rule_id": fix_info["rule_id"],
                            "file": fix_info["file"],
                            "language": fix_info["language"],
                            "start": fix_info["start"],
                            "end": fix_info["end"],
                            "replacement": fix_info["replacement"],
                            "applied": False,
                            "skipped_reason": None
                        })
                        applied_ranges.append((start_byte, end_byte))

                fix_results_by_file[file_rel_path] = fix_results

    # Build final output list
    output_items = []

    for match in all_matches:
        # Add match (without internal fields)
        output_match = {k: v for k, v in match.items() if not k.startswith("match_") and k != "fix"}
        output_items.append(output_match)

    for file_rel_path, fix_results in fix_results_by_file.items():
        for fix_result in fix_results:
            output_items.append(fix_result)

    # Sort output items
    output_items.sort(key=sort_key_output)

    # Output JSON Lines
    for item in output_items:
        print(json.dumps(item, separators=(',', ':')))

    sys.exit(0)


if __name__ == "__main__":
    main()
