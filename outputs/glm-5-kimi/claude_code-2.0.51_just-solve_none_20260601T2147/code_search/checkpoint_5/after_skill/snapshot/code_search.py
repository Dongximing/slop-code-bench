#!/usr/bin/env python3
"""Command-line code searcher for Python, JavaScript, C++, Rust, Java, Go, and Haskell codebases."""

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

import tree_sitter_python as tspython
import tree_sitter_javascript as tsjs
import tree_sitter_cpp as tscpp
import tree_sitter_rust as tsrust
import tree_sitter_java as tsjava
import tree_sitter_go as tsgo
import tree_sitter_haskell as tshaskell
from tree_sitter import Language, Parser, Node

_REGEX_FLAGS = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}

_ROOT_NODE_TYPES = frozenset({'module', 'program', 'translation_unit', 'source_file', 'haskell'})

_EXTENSION_TO_LANGUAGE = {
    ".py": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp",
    ".hh": "cpp", ".hpp": "cpp", ".hxx": "cpp",
    ".rs": "rust",
    ".java": "java",
    ".go": "go",
    ".hs": "haskell", ".lhs": "haskell",
}

_ALL_LANGUAGES = ["python", "javascript", "cpp", "rust", "java", "go", "haskell"]

_LANGUAGE_MODULES = {
    "python": tspython,
    "javascript": tsjs,
    "cpp": tscpp,
    "rust": tsrust,
    "java": tsjava,
    "go": tsgo,
    "haskell": tshaskell,
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
    "rust": {
        "program": "source_file",
        "module": "mod_item",
        "namespace": None,
        "import": "use_declaration",
        "export": None,
        "declaration": None,
        "definition": None,
        "variable_declaration": "let_declaration",
        "constant_declaration": "const_item",
        "type_declaration": None,
        "function_declaration": "function_item",
        "method_declaration": "function_item",
        "class_declaration": None,
        "interface_declaration": "trait_item",
        "struct_declaration": "struct_item",
        "enum_declaration": "enum_item",
        "field_declaration": "field_declaration",
        "type": ["primitive_type", "type_identifier"],
        "generic_type": None,
        "parameter": "parameters",
        "argument": "arguments",
        "block": "block",
        "statement": "expression_statement",
        "return_statement": "return_expression",
        "throw_statement": None,
        "break_statement": "break_expression",
        "continue_statement": "continue_expression",
        "if_statement": "if_expression",
        "else_clause": None,
        "switch_statement": "match_expression",
        "case_clause": "match_arm",
        "for_statement": "for_expression",
        "while_statement": "while_expression",
        "do_while_statement": None,
        "try_statement": None,
        "catch_clause": None,
        "finally_clause": None,
        "expression": ["binary_expression", "unary_expression", "call_expression", "return_expression", "break_expression", "continue_expression"],
        "assignment_expression": None,
        "call_expression": "call_expression",
        "member_expression": None,
        "subscript_expression": "index_expression",
        "binary_expression": "binary_expression",
        "unary_expression": "unary_expression",
        "conditional_expression": None,
        "literal": ["string_literal", "integer_literal", "boolean_literal", "float_literal"],
        "string_literal": "string_literal",
        "numeric_literal": ["integer_literal", "float_literal"],
        "boolean_literal": "boolean_literal",
        "null_literal": None,
        "array_literal": None,
        "object_literal": None,
        "annotation": None,
        "decorator": ["attribute_item", "inner_attribute_item"],
        "attribute": None,
        "access_modifier": "visibility_modifier",
        "operator": None,
        "identifier": ["identifier", "field_identifier", "type_identifier"],
        "comment": ["comment", "line_comment", "block_comment"],
    },
    "java": {
        "program": "program",
        "module": None,
        "namespace": "package_declaration",
        "import": "import_declaration",
        "export": None,
        "declaration": None,
        "definition": None,
        "variable_declaration": "local_variable_declaration",
        "constant_declaration": None,
        "type_declaration": None,
        "function_declaration": "method_declaration",
        "method_declaration": "method_declaration",
        "class_declaration": "class_declaration",
        "interface_declaration": "interface_declaration",
        "struct_declaration": None,
        "enum_declaration": "enum_declaration",
        "field_declaration": "field_declaration",
        "type": ["integral_type", "void_type", "type_identifier"],
        "generic_type": None,
        "parameter": ["formal_parameters", "formal_parameter"],
        "argument": "argument_list",
        "block": "block",
        "statement": "expression_statement",
        "return_statement": "return_statement",
        "throw_statement": "throw_statement",
        "break_statement": None,
        "continue_statement": None,
        "if_statement": "if_statement",
        "else_clause": None,
        "switch_statement": None,
        "case_clause": None,
        "for_statement": "for_statement",
        "while_statement": "while_statement",
        "do_while_statement": None,
        "try_statement": "try_statement",
        "catch_clause": "catch_clause",
        "finally_clause": "finally_clause",
        "expression": ["binary_expression", "method_invocation", "field_access", "assignment_expression"],
        "assignment_expression": None,
        "call_expression": "method_invocation",
        "member_expression": "field_access",
        "subscript_expression": None,
        "binary_expression": "binary_expression",
        "unary_expression": None,
        "conditional_expression": None,
        "literal": ["string_literal", "decimal_integer_literal", "decimal_floating_point_literal", "true", "false", "null"],
        "string_literal": "string_literal",
        "numeric_literal": ["decimal_integer_literal", "decimal_floating_point_literal"],
        "boolean_literal": ["true", "false"],
        "null_literal": "null",
        "array_literal": None,
        "object_literal": None,
        "annotation": ["marker_annotation", "annotation"],
        "decorator": None,
        "attribute": None,
        "access_modifier": "modifiers",
        "operator": None,
        "identifier": "identifier",
        "comment": "comment",
    },
    "go": {
        "program": "source_file",
        "module": None,
        "namespace": None,
        "import": "import_declaration",
        "export": None,
        "declaration": None,
        "definition": None,
        "variable_declaration": ["var_declaration", "short_var_declaration"],
        "constant_declaration": "const_declaration",
        "type_declaration": "type_declaration",
        "function_declaration": "function_declaration",
        "method_declaration": "method_declaration",
        "class_declaration": None,
        "interface_declaration": None,
        "struct_declaration": None,
        "enum_declaration": None,
        "field_declaration": "field_declaration",
        "type": "type_identifier",
        "generic_type": None,
        "parameter": "parameter_list",
        "argument": "argument_list",
        "block": "block",
        "statement": "expression_statement",
        "return_statement": "return_statement",
        "throw_statement": None,
        "break_statement": "break_statement",
        "continue_statement": "continue_statement",
        "if_statement": "if_statement",
        "else_clause": None,
        "switch_statement": None,
        "case_clause": None,
        "for_statement": "for_statement",
        "while_statement": None,
        "do_while_statement": None,
        "try_statement": None,
        "catch_clause": None,
        "finally_clause": None,
        "expression": ["call_expression", "binary_expression", "selector_expression", "unary_expression"],
        "assignment_expression": None,
        "call_expression": "call_expression",
        "member_expression": "selector_expression",
        "subscript_expression": "index_expression",
        "binary_expression": "binary_expression",
        "unary_expression": "unary_expression",
        "conditional_expression": None,
        "literal": ["interpreted_string_literal", "int_literal", "float_literal", "true", "false", "nil", "rune_literal"],
        "string_literal": "interpreted_string_literal",
        "numeric_literal": ["int_literal", "float_literal"],
        "boolean_literal": ["true", "false"],
        "null_literal": "nil",
        "array_literal": "composite_literal",
        "object_literal": "composite_literal",
        "annotation": None,
        "decorator": None,
        "attribute": None,
        "access_modifier": None,
        "operator": None,
        "identifier": ["identifier", "field_identifier", "package_identifier", "type_identifier"],
        "comment": "comment",
    },
    "haskell": {
        "program": "haskell",
        "module": "module",
        "namespace": None,
        "import": "import",
        "export": None,
        "declaration": None,
        "definition": None,
        "variable_declaration": "bind",
        "constant_declaration": None,
        "type_declaration": ["data_type", "newtype", "type_synomym"],
        "function_declaration": "function",
        "method_declaration": None,
        "class_declaration": "class",
        "interface_declaration": None,
        "struct_declaration": None,
        "enum_declaration": None,
        "field_declaration": None,
        "type": "name",
        "generic_type": None,
        "parameter": "patterns",
        "argument": None,
        "block": None,
        "statement": None,
        "return_statement": None,
        "throw_statement": None,
        "break_statement": None,
        "continue_statement": None,
        "if_statement": None,
        "else_clause": None,
        "switch_statement": None,
        "case_clause": None,
        "for_statement": None,
        "while_statement": None,
        "do_while_statement": None,
        "try_statement": None,
        "catch_clause": None,
        "finally_clause": None,
        "expression": ["apply", "infix", "prefix", "literal", "variable"],
        "assignment_expression": None,
        "call_expression": "apply",
        "member_expression": None,
        "subscript_expression": None,
        "binary_expression": "infix",
        "unary_expression": None,
        "conditional_expression": None,
        "literal": ["literal", "string", "integer", "unit"],
        "string_literal": "string",
        "numeric_literal": "integer",
        "boolean_literal": None,
        "null_literal": None,
        "array_literal": "list",
        "object_literal": None,
        "annotation": "signature",
        "decorator": None,
        "attribute": None,
        "access_modifier": None,
        "operator": None,
        "identifier": ["variable", "name", "constructor"],
        "comment": "comment",
    },
}


def get_parser(language: str) -> Parser:
    if language not in _PARSERS:
        lang_module = _LANGUAGE_MODULES[language]
        _PARSERS[language] = Parser(Language(lang_module.language()))
    return _PARSERS[language]


def _node_pos(node: Node) -> dict:
    return {"line": node.start_point.row + 1, "col": node.start_point.column + 1}


def _node_end_pos(node: Node) -> dict:
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
        meta_vars[name] = meta_vars.get(name, False) or optional
        return f"__META_{name}__"

    pattern = pattern.replace("$$", "__DOLLAR_DOLLAR__")
    pattern = re.sub(r'\$([A-Za-z_][A-Za-z0-9_]*)(\?)?', replace_meta, pattern)
    return pattern, meta_vars, all_names


def _parse_pattern(pattern: str, language: str) -> tuple[Node | None, set[str], dict[str, bool]]:
    """Parse a pattern string into an AST node, returning (node, meta_names, is_optional)."""
    transformed, is_optional, meta_names = _extract_metavariables(pattern)
    parser = get_parser(language)

    # For Go, wrap pattern in a function body for correct call expression parsing
    if language == "go":
        wrapped = f"func _() {{ {transformed} }}"
        tree = parser.parse(wrapped.encode('utf-8'))
        for node in _find_all_nodes(tree.root_node):
            if node.type == 'expression_statement' and '__META_' in _nt(node):
                for child in node.children:
                    if child.type not in ('ERROR', ';'):
                        return child, meta_names, is_optional
                if node.child_count > 0:
                    return node.children[0], meta_names, is_optional
        tree = parser.parse(transformed.encode('utf-8'))
    else:
        tree = parser.parse(transformed.encode('utf-8'))

    root = tree.root_node
    if root.child_count == 0:
        return None, meta_names, is_optional

    # Find first non-ERROR child
    content_node = next((child for child in root.children if child.type != 'ERROR'), root)

    # Unwrap expression_statement wrapper
    if content_node.type == 'expression_statement' and content_node.child_count > 0:
        content_node = content_node.children[0]

    # Unwrap Haskell declarations/top_splice wrapper
    if content_node.type == 'declarations' and content_node.child_count > 0:
        content_node = next((child for child in content_node.children if child.type != 'ERROR'), content_node)
        if content_node.type == 'top_splice' and content_node.child_count > 0:
            content_node = content_node.children[0]

    return content_node, meta_names, is_optional


_IDENTIFIER_TYPES = frozenset({
    'identifier', 'property_identifier', 'type_identifier',
    'field_identifier', 'package_identifier', 'variable', 'name',
    'module_id', 'constructor',
})
_IDENTIFIER_OR_PRINT = frozenset(_IDENTIFIER_TYPES | {'print'})


def _is_meta_placeholder(text: str) -> str | None:
    if text == "__DOLLAR_DOLLAR__":
        return "$$"
    if text.startswith("__META_") and text.endswith("__"):
        name = text[7:-2]
        if name:
            return name
    return None


def _types_compatible(p_type: str, s_type: str) -> bool:
    return p_type == s_type or (p_type in _IDENTIFIER_TYPES and s_type in _IDENTIFIER_OR_PRINT)


def _nt(node: Node) -> str:
    return node.text.decode('utf-8', errors='replace')


class _MatchCtx:
    """Shared context for pattern matching, bundling parameters that flow through
    the _match_pattern <-> _match_children recursion."""
    __slots__ = ('source_bytes', 'bindings', 'meta_names', 'is_optional')

    def __init__(self, source_bytes: bytes, meta_names: set[str], is_optional: dict[str, bool]):
        self.source_bytes = source_bytes
        self.bindings: dict[str, tuple[str, list[Node]]] = {}
        self.meta_names = meta_names
        self.is_optional = is_optional

    def save(self) -> dict[str, tuple[str, list[Node]]]:
        return dict(self.bindings)

    def restore(self, snapshot: dict[str, tuple[str, list[Node]]]) -> None:
        self.bindings.clear()
        self.bindings.update(snapshot)


def _match_pattern(pattern_node: Node, source_node: Node, ctx: _MatchCtx) -> bool:
    """Try to match a pattern AST node against a source AST node, updating bindings."""
    p_text = _nt(pattern_node)
    meta_name = _is_meta_placeholder(p_text)

    if meta_name:
        if meta_name == "$$":
            return _nt(source_node) == "$"
        s_text = _nt(source_node)
        if meta_name in ctx.bindings:
            return s_text == ctx.bindings[meta_name][0]
        ctx.bindings[meta_name] = (s_text, [source_node])
        return True

    if pattern_node.type != 'ERROR' and (source_node.type == 'ERROR' or not _types_compatible(pattern_node.type, source_node.type)):
        return False

    if pattern_node.child_count == 0:
        return p_text == _nt(source_node)

    return _match_children(list(pattern_node.children), 0, list(source_node.children), 0, ctx)


def _match_children(
    p_children: list[Node], p_idx: int,
    s_children: list[Node], s_idx: int,
    ctx: _MatchCtx
) -> bool:
    """Recursive child matching with backtracking for optional metavariables."""
    if p_idx >= len(p_children) and s_idx >= len(s_children):
        return True
    if p_idx >= len(p_children):
        return False
    if s_idx >= len(s_children):
        return all(
            (m := _is_meta_placeholder(_nt(child))) in ctx.meta_names
            and ctx.is_optional.get(m, False)
            for child in p_children[p_idx:]
        )

    p_node = p_children[p_idx]
    s_node = s_children[s_idx]

    p_text = _nt(p_node)
    meta_name = _is_meta_placeholder(p_text)

    # Optional metavariable: try skipping it first, then matching it
    if meta_name and meta_name != "$$" and ctx.is_optional.get(meta_name, False):
        saved = ctx.save()
        if _match_children(p_children, p_idx + 1, s_children, s_idx, ctx):
            return True
        ctx.restore(saved)

    if meta_name and meta_name != "$$":
        s_text = _nt(s_node)

        if meta_name in ctx.bindings:
            if ctx.bindings[meta_name][0] == s_text:
                ctx.bindings[meta_name] = (ctx.bindings[meta_name][0], ctx.bindings[meta_name][1] + [s_node])
                if _match_children(p_children, p_idx + 1, s_children, s_idx + 1, ctx):
                    return True
                ctx.bindings[meta_name] = (ctx.bindings[meta_name][0], ctx.bindings[meta_name][1][:-1])
            return False

        saved = ctx.save()
        ctx.bindings[meta_name] = (s_text, [s_node])
        if _match_children(p_children, p_idx + 1, s_children, s_idx + 1, ctx):
            return True

        # Try consuming multiple children for cases like Rust's token_tree
        for consume_count in range(2, len(s_children) - s_idx + 1):
            consumed_nodes = s_children[s_idx:s_idx + consume_count]
            consumed_text = ''.join(_nt(n) for n in consumed_nodes)
            ctx.bindings[meta_name] = (consumed_text, consumed_nodes)
            if _match_children(p_children, p_idx + 1, s_children, s_idx + consume_count, ctx):
                return True

        ctx.restore(saved)
        return False

    if meta_name == "$$":
        if _nt(s_node) != "$":
            return False
        return _match_children(p_children, p_idx + 1, s_children, s_idx + 1, ctx)

    if not _types_compatible(p_node.type, s_node.type):
        return False

    saved = ctx.save()
    if _match_pattern(p_node, s_node, ctx):
        if _match_children(p_children, p_idx + 1, s_children, s_idx + 1, ctx):
            return True

    ctx.restore(saved)
    return False


def _find_all_nodes(root: Node) -> list[Node]:
    nodes = []
    stack = [root]
    while stack:
        node = stack.pop()
        nodes.append(node)
        stack.extend(reversed(node.children))
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
        if source_node.type in _ROOT_NODE_TYPES:
            continue

        ctx = _MatchCtx(source_bytes, meta_names, is_optional)
        if _match_pattern(pattern_node, source_node, ctx):
            match_text = content[source_node.start_byte:source_node.end_byte]

            captures = {}
            for mn in sorted(ctx.bindings):
                bound_text, nodes = ctx.bindings[mn]
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
        kind = rule["kind"]
        kind_specific = {}
        if kind == "regex":
            flags = sum(_REGEX_FLAGS.get(f, 0) for f in rule.get("regex_flags", []))
            kind_specific["compiled"] = re.compile(rule["pattern"], flags)
        elif kind == "selector":
            kind_specific["selector"] = rule["selector"]
        else:
            kind_specific["pattern"] = rule["pattern"]

        entry = {"id": rule["id"], "kind": kind, "languages": rule.get("languages", _ALL_LANGUAGES)}
        entry |= kind_specific
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


def _pos_to_byte(content: str, line: int, col: int) -> int:
    """Convert a 1-indexed (line, col) position to a byte offset."""
    pos = 0
    for current in range(1, line):
        idx = content.find('\n', pos)
        if idx == -1:
            return len(content.encode('utf-8'))
        pos = idx + 1
    return len(content[:pos + col - 1].encode('utf-8'))


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


if __name__ == "__main__":
    main()
