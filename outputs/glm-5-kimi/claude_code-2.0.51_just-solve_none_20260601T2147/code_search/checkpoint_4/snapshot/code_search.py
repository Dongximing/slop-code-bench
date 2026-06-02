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

# Mapping from normalized selector names to tree-sitter node types per language.
# Values can be: None (not supported), a string (single type), or a list (multiple types).
_SELECTOR_MAP: Dict[str, Dict[str, Optional[Any]]] = {
    "python": {
        "program": "module",
        "module": "module",
        "namespace": None,
        "import": "import_statement",
        "export": None,
        "declaration": None,
        "definition": None,
        "variable_declaration": None,
        "constant_declaration": None,
        "type_declaration": None,
        "function_declaration": "function_definition",
        "method_declaration": "function_definition",
        "class_declaration": "class_definition",
        "interface_declaration": None,
        "struct_declaration": None,
        "enum_declaration": None,
        "field_declaration": None,
        "type": "type",
        "generic_type": None,
        "parameter": ["typed_parameter", "typed_default_parameter", "default_parameter", "dictionary_splat_pattern", "list_splat_pattern", "keyword_separator"],
        "argument": "argument_list",
        "block": "block",
        "statement": "expression_statement",
        "return_statement": "return_statement",
        "throw_statement": "raise_statement",
        "break_statement": "break_statement",
        "continue_statement": "continue_statement",
        "if_statement": "if_statement",
        "else_clause": "else_clause",
        "switch_statement": None,
        "case_clause": None,
        "for_statement": "for_statement",
        "while_statement": "while_statement",
        "do_while_statement": None,
        "try_statement": "try_statement",
        "catch_clause": "except_clause",
        "finally_clause": "finally_clause",
        "expression": ["call", "binary_operator", "comparison_operator", "assignment", "subscript", "attribute", "conditional_expression", "unary_operator", "boolean_operator"],
        "assignment_expression": "assignment",
        "call_expression": "call",
        "member_expression": "attribute",
        "subscript_expression": "subscript",
        "binary_expression": ["binary_operator", "comparison_operator", "boolean_operator"],
        "unary_expression": "unary_operator",
        "conditional_expression": "conditional_expression",
        "literal": ["integer", "string", "true", "false", "none", "list", "dictionary", "float"],
        "string_literal": "string",
        "numeric_literal": ["integer", "float"],
        "boolean_literal": ["true", "false"],
        "null_literal": "none",
        "array_literal": "list",
        "object_literal": "dictionary",
        "annotation": None,
        "decorator": "decorator",
        "attribute": "attribute",
        "access_modifier": None,
        "operator": None,
        "identifier": "identifier",
        "comment": "comment",
    },
    "javascript": {
        "program": "program",
        "module": "program",
        "namespace": None,
        "import": "import_statement",
        "export": "export_statement",
        "declaration": ["declaration", "lexical_declaration"],
        "definition": None,
        "variable_declaration": ["lexical_declaration", "variable_declaration"],
        "constant_declaration": "lexical_declaration",
        "type_declaration": None,
        "function_declaration": "function_declaration",
        "method_declaration": "method_definition",
        "class_declaration": "class_declaration",
        "interface_declaration": None,
        "struct_declaration": None,
        "enum_declaration": None,
        "field_declaration": ["field_definition", "public_field_definition"],
        "type": None,
        "generic_type": None,
        "parameter": ["formal_parameters", "identifier"],
        "argument": "arguments",
        "block": "statement_block",
        "statement": ["expression_statement", "lexical_declaration", "variable_declaration"],
        "return_statement": "return_statement",
        "throw_statement": "throw_statement",
        "break_statement": "break_statement",
        "continue_statement": None,
        "if_statement": "if_statement",
        "else_clause": "else_clause",
        "switch_statement": "switch_statement",
        "case_clause": ["switch_case", "switch_default"],
        "for_statement": "for_statement",
        "while_statement": "while_statement",
        "do_while_statement": "do_statement",
        "try_statement": "try_statement",
        "catch_clause": "catch_clause",
        "finally_clause": "finally_clause",
        "expression": ["call_expression", "binary_expression", "assignment_expression", "member_expression", "subscript_expression", "unary_expression", "conditional_expression"],
        "assignment_expression": "assignment_expression",
        "call_expression": "call_expression",
        "member_expression": "member_expression",
        "subscript_expression": "subscript_expression",
        "binary_expression": "binary_expression",
        "unary_expression": "unary_expression",
        "conditional_expression": "conditional_expression",
        "literal": ["string", "number", "true", "false", "null", "undefined", "array", "object"],
        "string_literal": "string",
        "numeric_literal": "number",
        "boolean_literal": ["true", "false"],
        "null_literal": ["null", "undefined"],
        "array_literal": "array",
        "object_literal": "object",
        "annotation": None,
        "decorator": "decorator",
        "attribute": None,
        "access_modifier": None,
        "operator": None,
        "identifier": "identifier",
        "comment": "comment",
    },
    "cpp": {
        "program": "translation_unit",
        "module": "translation_unit",
        "namespace": "namespace_definition",
        "import": "preproc_include",
        "export": None,
        "declaration": "declaration",
        "definition": ["function_definition", "class_specifier", "struct_specifier", "enum_specifier"],
        "variable_declaration": "declaration",
        "constant_declaration": "declaration",
        "type_declaration": None,
        "function_declaration": "function_definition",
        "method_declaration": "function_definition",
        "class_declaration": "class_specifier",
        "interface_declaration": None,
        "struct_declaration": "struct_specifier",
        "enum_declaration": "enum_specifier",
        "field_declaration": "field_declaration",
        "type": ["primitive_type", "type_identifier"],
        "generic_type": "template_declaration",
        "parameter": "parameter_declaration",
        "argument": "argument_list",
        "block": "compound_statement",
        "statement": ["expression_statement", "compound_statement"],
        "return_statement": "return_statement",
        "throw_statement": "throw_statement",
        "break_statement": "break_statement",
        "continue_statement": None,
        "if_statement": "if_statement",
        "else_clause": "else_clause",
        "switch_statement": "switch_statement",
        "case_clause": "case_statement",
        "for_statement": "for_statement",
        "while_statement": "while_statement",
        "do_while_statement": "do_statement",
        "try_statement": "try_statement",
        "catch_clause": "catch_clause",
        "finally_clause": None,
        "expression": ["call_expression", "binary_expression", "assignment_expression", "parenthesized_expression", "unary_expression", "conditional_expression"],
        "assignment_expression": "assignment_expression",
        "call_expression": "call_expression",
        "member_expression": "field_expression",
        "subscript_expression": "subscript_expression",
        "binary_expression": "binary_expression",
        "unary_expression": "unary_expression",
        "conditional_expression": "conditional_expression",
        "literal": ["string_literal", "number_literal", "true", "false", "null"],
        "string_literal": "string_literal",
        "numeric_literal": "number_literal",
        "boolean_literal": ["true", "false"],
        "null_literal": "null",
        "array_literal": "initializer_list",
        "object_literal": "initializer_list",
        "annotation": None,
        "decorator": None,
        "attribute": "attribute",
        "access_modifier": "access_specifier",
        "operator": None,
        "identifier": ["identifier", "field_identifier", "type_identifier"],
        "comment": "comment",
    },
}


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


def _find_selector_matches(
    content: str,
    selector: str,
    language: str,
    rule_id: str,
    file_path: str
) -> List[Dict[str, Any]]:
    """Find all AST nodes matching a selector for the given language."""
    lang_map = _SELECTOR_MAP.get(language, {})
    ts_types = lang_map.get(selector)

    if ts_types is None:
        return []

    if isinstance(ts_types, str):
        ts_types = [ts_types]

    parser = get_parser(language)
    source_bytes = content.encode('utf-8')
    tree = parser.parse(source_bytes)

    matches = []
    for node in _find_all_nodes(tree.root_node):
        if node.type in ts_types:
            start_byte = node.start_byte
            end_byte = node.end_byte
            match_text = content[start_byte:end_byte]

            start_line, start_col = _line_col(content, start_byte)
            end_line, end_col = _line_col(content, end_byte)

            matches.append({
                "rule_id": rule_id,
                "file": file_path,
                "language": language,
                "start": {"line": start_line, "col": start_col},
                "end": {"line": end_line, "col": end_col},
                "match": match_text,
            })

    return matches


def _expand_template(template: str, match_text: str, captures: Dict[str, Any]) -> str:
    """Expand a fix template, replacing $NAME, $MATCH, and $$ placeholders."""
    # First replace $$ with a sentinel to protect literal dollar signs
    sentinel = "\x00DOLLAR\x00"
    result = template.replace("$$", sentinel)

    # Replace $MATCH
    result = result.replace("$MATCH", match_text)

    # Replace capture placeholders ($NAME) - longest first to avoid partial matches
    # Variable names are case-sensitive
    capture_names = sorted(captures.keys(), key=len, reverse=True)
    for name in capture_names:
        # name already has $ prefix from captures dict (e.g., "$ARG")
        capture_text = captures[name]["text"]
        result = result.replace(name, capture_text)

    # Restore literal dollar signs
    result = result.replace(sentinel, "$")

    return result


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
        elif rule["kind"] == "selector":
            entry["selector"] = rule["selector"]
        else:
            entry["pattern"] = rule["pattern"]

        # Parse fix object if present
        if "fix" in rule:
            entry["fix"] = rule["fix"]

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

    if kind == "selector":
        return _find_selector_matches(content, prepared["selector"], language, rule_id, file_path)

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
) -> Tuple[List[Dict[str, Any]], str]:
    try:
        with open(full_path, "r", encoding=encoding) as f:
            content = f.read()
    except (UnicodeDecodeError, OSError):
        return [], ""

    all_matches = []
    for rule in rules:
        if language in rule["languages"]:
            all_matches.extend(_find_matches(content, rule, file_path, language))
    return all_matches, content


def _sort_key(line_data: Dict[str, Any]) -> Tuple:
    """Sort key for output lines: file, start.line, start.col, rule_id, type_priority."""
    is_fix = line_data.get("event") == "fix"
    # Match lines come before fix lines when tied
    type_priority = 1 if is_fix else 0
    return (
        line_data.get("file", ""),
        line_data["start"]["line"],
        line_data["start"]["col"],
        line_data["rule_id"],
        type_priority,
    )


def main():
    parser = argparse.ArgumentParser(description="Code searcher for Python, JavaScript, and C++ codebases")
    parser.add_argument("root_dir", help="Path to the codebase to scan")
    parser.add_argument("--rules", required=True, help="Path to a JSON array of rules")
    parser.add_argument("--encoding", default="utf-8", help="File encoding (default: utf-8)")

    fix_group = parser.add_mutually_exclusive_group()
    fix_group.add_argument("--dry-run", action="store_true", help="Preview fixes without writing to disk")
    fix_group.add_argument("--apply-fixes", action="store_true", help="Apply fixes and write to disk")

    args = parser.parse_args()

    root_dir = Path(args.root_dir).resolve()
    with open(args.rules, "r", encoding="utf-8") as f:
        raw_rules = json.load(f)
    rules = _prepare_rules(raw_rules)

    mode = "normal"
    if args.dry_run:
        mode = "dry_run"
    elif args.apply_fixes:
        mode = "apply_fixes"

    # Collect all matches and file contents
    file_contents: Dict[str, str] = {}
    all_matches = []
    for dirpath, _, filenames in os.walk(root_dir):
        for filename in filenames:
            full_path = Path(dirpath) / filename
            language = _EXTENSION_TO_LANGUAGE.get(full_path.suffix.lower())
            if language is None:
                continue
            rel_path = full_path.relative_to(root_dir).as_posix()
            matches, content = scan_file(rel_path, full_path, language, rules, args.encoding)
            all_matches.extend(matches)
            if matches and mode != "normal":
                file_contents[rel_path] = content

    # Build output lines
    output_lines: List[Dict[str, Any]] = []

    for match in all_matches:
        output_lines.append(match)

    # If we're in fix mode, generate fix lines
    if mode != "normal":
        # Build a lookup for rules with fixes
        rule_fixes: Dict[str, Dict[str, Any]] = {}
        for rule in rules:
            if "fix" in rule:
                rule_fixes[rule["id"]] = rule["fix"]

        # Generate fix candidates from matches
        fix_candidates: List[Dict[str, Any]] = []
        for match in all_matches:
            rule_id = match["rule_id"]
            if rule_id in rule_fixes:
                fix_info = rule_fixes[rule_id]
                captures = match.get("captures", {})
                replacement = _expand_template(fix_info["template"], match["match"], captures)

                fix_line = {
                    "event": "fix",
                    "rule_id": rule_id,
                    "file": match["file"],
                    "language": match["language"],
                    "start": match["start"],
                    "end": match["end"],
                    "replacement": replacement,
                    "applied": False,
                    "skipped_reason": None,
                }
                fix_candidates.append(fix_line)
                output_lines.append(fix_line)

    # Sort all output lines
    output_lines.sort(key=_sort_key)

    if mode == "apply_fixes":
        # Apply fixes with conflict resolution
        # Sort fix candidates: by file, then start.line, then start.col, then rule_id
        fix_candidates.sort(key=lambda f: (
            f["file"],
            f["start"]["line"],
            f["start"]["col"],
            f["rule_id"],
        ))

        # Group fixes by file and check for overlaps
        applied_ranges: Dict[str, List[Tuple[int, int]]] = {}
        files_to_write: Dict[str, List[Tuple[int, int, str]]] = {}

        for fix in fix_candidates:
            file_path = fix["file"]
            if file_path not in file_contents:
                continue

            content = file_contents[file_path]
            # Convert line/col back to byte offsets
            start_byte = _pos_to_byte(content, fix["start"]["line"], fix["start"]["col"])
            end_byte = _pos_to_byte(content, fix["end"]["line"], fix["end"]["col"])

            # Check for overlap with already-applied fixes
            ranges = applied_ranges.get(file_path, [])
            overlap = False
            for (applied_start, applied_end) in ranges:
                if start_byte < applied_end and end_byte > applied_start:
                    overlap = True
                    break

            if overlap:
                fix["applied"] = False
                fix["skipped_reason"] = "overlap"
            else:
                fix["applied"] = True
                if file_path not in applied_ranges:
                    applied_ranges[file_path] = []
                applied_ranges[file_path].append((start_byte, end_byte))
                if file_path not in files_to_write:
                    files_to_write[file_path] = []
                files_to_write[file_path].append((start_byte, end_byte, fix["replacement"]))

        # Write files
        for file_path, replacements in files_to_write.items():
            full_path = root_dir / file_path
            content = file_contents[file_path]
            # Sort replacements by start_byte descending so we apply from end to start
            replacements.sort(key=lambda r: r[0], reverse=True)
            content_bytes = content.encode('utf-8')
            for start_byte, end_byte, replacement in replacements:
                content_bytes = content_bytes[:start_byte] + replacement.encode('utf-8') + content_bytes[end_byte:]
            with open(full_path, "wb") as f:
                f.write(content_bytes)

    # Output all lines
    for line in output_lines:
        print(json.dumps(line, separators=(',', ':')))


def _pos_to_byte(content: str, line: int, col: int) -> int:
    """Convert a (line, col) position to a byte offset in the content string."""
    current_line = 1
    line_start = 0
    for i, ch in enumerate(content):
        if current_line == line:
            # We're at the right line; compute byte offset
            char_offset = line_start + (col - 1)
            # Return the byte offset, not character offset
            return len(content[:char_offset].encode('utf-8'))
        if ch == '\n':
            current_line += 1
            line_start = i + 1
    # If we reach here, the position is at the end
    if current_line == line:
        char_offset = line_start + (col - 1)
        return len(content[:char_offset].encode('utf-8'))
    return len(content.encode('utf-8'))


if __name__ == "__main__":
    main()
