#!/usr/bin/env python3
"""Command-line code searcher for Python, JavaScript, and C++ codebases."""

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

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

_PARSERS: dict[str, Parser] = {}

# Mapping from normalized selector names to tree-sitter node types per language.
# Values: None = unsupported, str = single type, list = multiple types.
_SELECTOR_MAP: dict[str, dict[str, Any]] = {
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


def _node_pos(node: Node) -> dict:
    """Return 1-indexed start position from a tree-sitter node."""
    return {"line": node.start_point.row + 1, "col": node.start_point.column + 1}


def _node_end_pos(node: Node) -> dict:
    """Return 1-indexed end position from a tree-sitter node."""
    return {"line": node.end_point.row + 1, "col": node.end_point.column + 1}


def _extract_metavariables(pattern: str) -> tuple[str, dict[str, bool], set[str]]:
    """Replace $NAME and $NAME? placeholders with valid identifiers.

    Returns (transformed_pattern, {name: is_optional}, all_names).
    """
    meta_vars: dict[str, bool] = {}
    all_names: set[str] = set()

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


def _parse_pattern(pattern: str, language: str) -> tuple[Node | None, set[str], dict[str, bool]]:
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


_IDENTIFIER_TYPES = frozenset({'identifier', 'property_identifier', 'type_identifier'})
_IDENTIFIER_OR_PRINT = frozenset({'identifier', 'property_identifier', 'type_identifier', 'print'})


def _is_meta_placeholder(text: str) -> str | None:
    """If text is a metavar placeholder, return the original name; else None."""
    if text == "__DOLLAR_DOLLAR__":
        return "$$"
    if text.startswith("__META_") and text.endswith("__"):
        name = text[7:-2]
        if name and all(c.isalnum() or c == '_' for c in name):
            return name
    return None


def _types_compatible(p_type: str, s_type: str) -> bool:
    return p_type == s_type or (p_type in _IDENTIFIER_TYPES and s_type in _IDENTIFIER_OR_PRINT)


def _match_pattern(
    pattern_node: Node,
    source_node: Node,
    source_bytes: bytes,
    bindings: dict[str, tuple[str, list[Node]]],
    meta_names: set[str],
    is_optional: dict[str, bool]
) -> bool:
    """Try to match a pattern AST node against a source AST node, updating bindings."""
    p_text = pattern_node.text.decode('utf-8', errors='replace')

    meta_name = _is_meta_placeholder(p_text)

    if meta_name:
        if meta_name == "$$":
            return source_node.text.decode('utf-8', errors='replace') == "$"
        s_text = source_node.text.decode('utf-8', errors='replace')
        if meta_name in bindings:
            return s_text == bindings[meta_name][0]
        bindings[meta_name] = (s_text, [source_node])
        return True

    # Type compatibility check
    if pattern_node.type == 'ERROR':
        pass
    elif source_node.type == 'ERROR':
        return False
    elif not _types_compatible(pattern_node.type, source_node.type):
        return False

    if pattern_node.child_count == 0:
        return p_text == source_node.text.decode('utf-8', errors='replace')

    return _match_children_recursive(
        list(pattern_node.children), 0,
        list(source_node.children), 0,
        source_bytes, bindings, meta_names, is_optional
    )


def _match_children_recursive(
    p_children: list[Node],
    p_idx: int,
    s_children: list[Node],
    s_idx: int,
    source_bytes: bytes,
    bindings: dict[str, tuple[str, list[Node]]],
    meta_names: set[str],
    is_optional: dict[str, bool]
) -> bool:
    """Recursive child matching with backtracking for optional metavariables."""
    if p_idx >= len(p_children) and s_idx >= len(s_children):
        return True
    if p_idx >= len(p_children):
        return False
    if s_idx >= len(s_children):
        return all(
            _is_meta_placeholder(p_children[i].text.decode('utf-8', errors='replace')) in meta_names
            and is_optional.get(_is_meta_placeholder(p_children[i].text.decode('utf-8', errors='replace')), False)
            for i in range(p_idx, len(p_children))
        )

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
        if s_node.text.decode('utf-8', errors='replace') != "$":
            return False
        return _match_children_recursive(
            p_children, p_idx + 1, s_children, s_idx + 1,
            source_bytes, bindings, meta_names, is_optional
        )

    # Structural node matching
    if not _types_compatible(p_node.type, s_node.type):
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


def _find_all_nodes(root: Node) -> list[Node]:
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
) -> list[dict[str, Any]]:
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

        bindings: dict[str, tuple[str, list[Node]]] = {}
        if _match_pattern(pattern_node, source_node, source_bytes, bindings, meta_names, is_optional):
            match_text = content[source_node.start_byte:source_node.end_byte]

            captures = {}
            for mn in sorted(bindings):
                bound_text, nodes = bindings[mn]
                ranges = [{"start": _node_pos(n), "end": _node_end_pos(n)} for n in nodes]
                captures[f"${mn}"] = {"text": bound_text, "ranges": ranges}

            matches.append({
                "rule_id": rule_id,
                "file": "",
                "language": language,
                "start": _node_pos(source_node),
                "end": _node_end_pos(source_node),
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
) -> list[dict[str, Any]]:
    """Find all AST nodes matching a selector for the given language."""
    lang_map = _SELECTOR_MAP.get(language, {})
    ts_types = lang_map.get(selector)

    if ts_types is None:
        return []

    if isinstance(ts_types, str):
        ts_types = [ts_types]

    parser = get_parser(language)
    tree = parser.parse(content.encode('utf-8'))

    return [
        {
            "rule_id": rule_id,
            "file": file_path,
            "language": language,
            "start": _node_pos(node),
            "end": _node_end_pos(node),
            "match": content[node.start_byte:node.end_byte],
        }
        for node in _find_all_nodes(tree.root_node)
        if node.type in ts_types
    ]


def _expand_template(template: str, match_text: str, captures: dict[str, Any]) -> str:
    """Expand a fix template, replacing $NAME, $MATCH, and $$ placeholders."""
    sentinel = "\x00DOLLAR\x00"
    result = template.replace("$$", sentinel)
    result = result.replace("$MATCH", match_text)
    for name in sorted(captures.keys(), key=len, reverse=True):
        result = result.replace(name, captures[name]["text"])
    return result.replace(sentinel, "$")


def _prepare_rules(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
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

        if "fix" in rule:
            entry["fix"] = rule["fix"]

        prepared.append(entry)
    return prepared


def _find_matches(
    content: str,
    prepared: dict[str, Any],
    file_path: str,
    language: str
) -> list[dict[str, Any]]:
    rule_id = prepared["id"]
    kind = prepared["kind"]

    if kind == "selector":
        return _find_selector_matches(content, prepared["selector"], language, rule_id, file_path)

    if kind == "pattern":
        matches = _find_pattern_matches(content, prepared["pattern"], language, rule_id)
        for m in matches:
            m["file"] = file_path
        return matches

    # For regex/exact, we need byte offset to line/col conversion
    lines = content.split('\n')
    line_offsets = [0]
    for line in lines:
        line_offsets.append(line_offsets[-1] + len(line) + 1)

    def byte_to_pos(byte_offset: int) -> dict:
        for i, offset in enumerate(line_offsets):
            if offset > byte_offset:
                return {"line": i, "col": byte_offset - line_offsets[i - 1] + 1}
        return {"line": len(lines), "col": len(lines[-1]) + 1 if lines else 1}

    if kind == "regex":
        hits = [(m.start(), m.end(), m.group()) for m in prepared["compiled"].finditer(content)]
    else:  # exact
        pattern = prepared["pattern"]
        hits = []
        start = 0
        while (pos := content.find(pattern, start)) != -1:
            hits.append((pos, pos + len(pattern), pattern))
            start = pos + 1

    return [
        {
            "rule_id": rule_id, "file": file_path, "language": language,
            "start": byte_to_pos(s), "end": byte_to_pos(e),
            "match": t,
        }
        for s, e, t in hits
    ]


def scan_file(
    file_path: str,
    full_path: Path,
    language: str,
    rules: list[dict[str, Any]],
    encoding: str
) -> tuple[list[dict[str, Any]], str]:
    try:
        with open(full_path, "r", encoding=encoding) as f:
            content = f.read()
    except (UnicodeDecodeError, OSError):
        return [], ""

    return [
        match
        for rule in rules
        if language in rule["languages"]
        for match in _find_matches(content, rule, file_path, language)
    ], content


def _sort_key(line_data: dict[str, Any]) -> tuple:
    return (
        line_data.get("file", ""),
        line_data["start"]["line"],
        line_data["start"]["col"],
        line_data["rule_id"],
        1 if line_data.get("event") == "fix" else 0,
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
        rules = _prepare_rules(json.load(f))

    mode = "apply_fixes" if args.apply_fixes else ("dry_run" if args.dry_run else "normal")

    file_contents: dict[str, str] = {}
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

    output_lines: list[dict[str, Any]] = list(all_matches)

    if mode != "normal":
        rule_fixes = {r["id"]: r["fix"] for r in rules if "fix" in r}
        fix_candidates = []
        for match in all_matches:
            if match["rule_id"] in rule_fixes:
                fix_info = rule_fixes[match["rule_id"]]
                replacement = _expand_template(fix_info["template"], match["match"], match.get("captures", {}))
                fix_line = {
                    "event": "fix",
                    "rule_id": match["rule_id"],
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

    output_lines.sort(key=_sort_key)

    if mode == "apply_fixes":
        fix_candidates.sort(key=lambda f: (f["file"], f["start"]["line"], f["start"]["col"], f["rule_id"]))

        applied_ranges: dict[str, list[tuple[int, int]]] = {}
        files_to_write: dict[str, list[tuple[int, int, str]]] = {}

        for fix in fix_candidates:
            file_path = fix["file"]
            if file_path not in file_contents:
                continue

            content = file_contents[file_path]
            start_byte = _pos_to_byte(content, fix["start"]["line"], fix["start"]["col"])
            end_byte = _pos_to_byte(content, fix["end"]["line"], fix["end"]["col"])

            ranges = applied_ranges.get(file_path, [])
            if any(start_byte < ae and end_byte > as_ for (as_, ae) in ranges):
                fix["skipped_reason"] = "overlap"
            else:
                fix["applied"] = True
                applied_ranges.setdefault(file_path, []).append((start_byte, end_byte))
                files_to_write.setdefault(file_path, []).append((start_byte, end_byte, fix["replacement"]))

        for file_path, replacements in files_to_write.items():
            content = file_contents[file_path]
            content_bytes = content.encode('utf-8')
            for start_byte, end_byte, replacement in sorted(replacements, key=lambda r: r[0], reverse=True):
                content_bytes = content_bytes[:start_byte] + replacement.encode('utf-8') + content_bytes[end_byte:]
            with open(root_dir / file_path, "wb") as f:
                f.write(content_bytes)

    for line in output_lines:
        print(json.dumps(line, separators=(',', ':')))


def _pos_to_byte(content: str, line: int, col: int) -> int:
    """Convert a 1-indexed (line, col) position to a byte offset."""
    current_line = 1
    line_start = 0
    for i, ch in enumerate(content):
        if current_line == line:
            return len(content[:line_start + col - 1].encode('utf-8'))
        if ch == '\n':
            current_line += 1
            line_start = i + 1
    if current_line == line:
        return len(content[:line_start + col - 1].encode('utf-8'))
    return len(content.encode('utf-8'))


if __name__ == "__main__":
    main()
