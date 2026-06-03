#!/usr/bin/env python3
"""
Code searcher for Python, JavaScript, and C++ codebases.
Searches for patterns defined in rules and outputs matches as JSON Lines.
Supports auto-fixes with --dry-run and --apply-fixes options.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional

import tree_sitter_python
import tree_sitter_javascript
import tree_sitter_cpp
import tree_sitter


# Language mappings
LANGUAGE_PARSERS = {
    'python': tree_sitter_python,
    'javascript': tree_sitter_javascript,
    'cpp': tree_sitter_cpp,
}

LANGUAGE_EXTENSIONS = {
    '.py': 'python',
    '.js': 'javascript',
    '.cpp': 'cpp',
    '.hpp': 'cpp',
    '.cc': 'cpp',
    '.cxx': 'cpp',
    '.h': 'cpp',
    '.c': 'cpp',
}

# Allowed selector node types
ALLOWED_SELECTORS = {
    'program', 'module', 'namespace', 'import', 'export', 'declaration',
    'definition', 'variable_declaration', 'constant_declaration',
    'type_declaration', 'function_declaration', 'method_declaration',
    'class_declaration', 'interface_declaration', 'struct_declaration',
    'enum_declaration', 'field_declaration', 'type', 'generic_type',
    'parameter', 'argument', 'block', 'statement', 'return_statement',
    'throw_statement', 'break_statement', 'continue_statement',
    'if_statement', 'else_clause', 'switch_statement', 'case_clause',
    'for_statement', 'while_statement', 'do_while_statement',
    'try_statement', 'catch_clause', 'finally_clause', 'expression',
    'assignment_expression', 'call_expression', 'member_expression',
    'subscript_expression', 'binary_expression', 'unary_expression',
    'conditional_expression', 'literal', 'string_literal',
    'numeric_literal', 'boolean_literal', 'null_literal', 'array_literal',
    'object_literal', 'annotation', 'decorator', 'attribute',
    'access_modifier', 'operator', 'identifier', 'comment'
}

# Node type mappings for each language
NODE_TYPE_MAPPINGS = {
    'python': {
        'program': 'module',
        'module': 'module',
        'import': 'import_statement',
        'declaration': None,
        'definition': None,
        'variable_declaration': None,
        'constant_declaration': None,
        'type_declaration': 'type_alias_statement',
        'function_declaration': 'function_definition',
        'method_declaration': 'function_definition',
        'class_declaration': 'class_definition',
        'interface_declaration': None,
        'struct_declaration': None,
        'enum_declaration': None,
        'field_declaration': None,
        'type': None,
        'generic_type': None,
        'parameter': 'identifier',
        'argument': 'argument_list',
        'block': 'block',
        'statement': None,
        'return_statement': 'return_statement',
        'throw_statement': 'raise_statement',
        'break_statement': 'break_statement',
        'continue_statement': 'continue_statement',
        'if_statement': 'if_statement',
        'else_clause': 'else_clause',
        'switch_statement': None,
        'case_clause': None,
        'for_statement': 'for_statement',
        'while_statement': 'while_statement',
        'do_while_statement': None,
        'try_statement': 'try_statement',
        'catch_clause': 'except_clause',
        'finally_clause': 'finally_clause',
        'expression': 'expression_statement',
        'assignment_expression': 'assignment',
        'call_expression': 'call',
        'member_expression': 'attribute',
        'subscript_expression': 'subscript',
        'binary_expression': 'binary_operator',
        'unary_expression': 'unary_operator',
        'conditional_expression': 'conditional_expression',
        'literal': None,
        'string_literal': 'string',
        'numeric_literal': 'integer',
        'boolean_literal': 'true',
        'null_literal': 'none',
        'array_literal': 'list',
        'object_literal': 'dictionary',
        'annotation': None,
        'decorator': 'decorator',
        'attribute': 'attribute',
        'access_modifier': None,
        'operator': None,
        'identifier': 'identifier',
        'comment': 'comment',
    },
    'javascript': {
        'program': 'program',
        'module': 'program',
        'namespace': None,
        'import': 'import_statement',
        'export': 'export_statement',
        'declaration': None,
        'definition': None,
        'variable_declaration': 'variable_declaration',
        'constant_declaration': 'variable_declaration',
        'type_declaration': None,
        'function_declaration': 'function_declaration',
        'method_declaration': 'method_definition',
        'class_declaration': 'class_declaration',
        'interface_declaration': None,
        'struct_declaration': None,
        'enum_declaration': None,
        'field_declaration': 'field_definition',
        'type': 'type_identifier',
        'generic_type': 'generic_type',
        'parameter': 'identifier',
        'argument': 'arguments',
        'block': 'statement_block',
        'statement': None,
        'return_statement': 'return_statement',
        'throw_statement': 'throw_statement',
        'break_statement': 'break_statement',
        'continue_statement': 'continue_statement',
        'if_statement': 'if_statement',
        'else_clause': 'else_clause',
        'switch_statement': 'switch_statement',
        'case_clause': 'switch_case',
        'for_statement': 'for_statement',
        'while_statement': 'while_statement',
        'do_while_statement': 'do_statement',
        'try_statement': 'try_statement',
        'catch_clause': 'catch_clause',
        'finally_clause': 'finally_clause',
        'expression': 'expression_statement',
        'assignment_expression': 'assignment_expression',
        'call_expression': 'call_expression',
        'member_expression': 'member_expression',
        'subscript_expression': 'subscript_expression',
        'binary_expression': 'binary_expression',
        'unary_expression': 'unary_expression',
        'conditional_expression': 'ternary_expression',
        'literal': None,
        'string_literal': 'string',
        'numeric_literal': 'number',
        'boolean_literal': 'true',
        'null_literal': 'null',
        'array_literal': 'array',
        'object_literal': 'object',
        'annotation': None,
        'decorator': 'decorator',
        'attribute': 'property_identifier',
        'access_modifier': None,
        'operator': None,
        'identifier': 'identifier',
        'comment': 'comment',
    },
    'cpp': {
        'program': 'translation_unit',
        'module': 'translation_unit',
        'namespace': 'namespace_definition',
        'import': None,
        'export': None,
        'declaration': 'declaration',
        'definition': 'definition',
        'variable_declaration': 'declaration',
        'constant_declaration': 'declaration',
        'type_declaration': 'type_definition',
        'function_declaration': 'function_declaration',
        'method_declaration': 'function_definition',
        'class_declaration': 'class_specifier',
        'interface_declaration': None,
        'struct_declaration': 'struct_specifier',
        'enum_declaration': 'enum_specifier',
        'field_declaration': 'field_declaration',
        'type': 'type_identifier',
        'generic_type': 'template_type',
        'parameter': 'parameter_declaration',
        'argument': 'argument_list',
        'block': 'compound_statement',
        'statement': None,
        'return_statement': 'return_statement',
        'throw_statement': 'throw_statement',
        'break_statement': 'break_statement',
        'continue_statement': 'continue_statement',
        'if_statement': 'if_statement',
        'else_clause': 'else_clause',
        'switch_statement': 'switch_statement',
        'case_clause': 'case_statement',
        'for_statement': 'for_statement',
        'while_statement': 'while_statement',
        'do_while_statement': 'do_statement',
        'try_statement': 'try_statement',
        'catch_clause': 'catch_clause',
        'finally_clause': None,
        'expression': 'expression_statement',
        'assignment_expression': 'assignment_expression',
        'call_expression': 'call_expression',
        'member_expression': 'field_expression',
        'subscript_expression': 'subscript_expression',
        'binary_expression': 'binary_expression',
        'unary_expression': 'unary_expression',
        'conditional_expression': 'conditional_expression',
        'literal': None,
        'string_literal': 'string_literal',
        'numeric_literal': 'number_literal',
        'boolean_literal': 'true',
        'null_literal': 'nullptr',
        'array_literal': 'initializer_list',
        'object_literal': 'initializer_list',
        'annotation': None,
        'decorator': 'attribute_specifier',
        'attribute': 'attribute',
        'access_modifier': 'access_specifier',
        'operator': None,
        'identifier': 'identifier',
        'comment': 'comment',
    }
}


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description='Search codebase for patterns')
    parser.add_argument('root_dir', type=str, help='Path to the codebase to scan')
    parser.add_argument('--rules', type=str, required=True, help='Path to JSON rules file')
    parser.add_argument('--encoding', type=str, default='utf-8', help='File encoding (default: utf-8)')

    # Mutually exclusive fix options
    fix_group = parser.add_mutually_exclusive_group()
    fix_group.add_argument('--dry-run', action='store_true', help='Preview fixes without writing to disk')
    fix_group.add_argument('--apply-fixes', action='store_true', help='Apply fixes to files')

    return parser.parse_args()


def validate_rule(rule: Dict[str, Any], rule_ids: set) -> None:
    """Validate a single rule object."""
    if 'id' not in rule or not isinstance(rule['id'], str) or not rule['id']:
        raise ValueError(f"Rule must have a non-empty string 'id' field")

    if rule['id'] in rule_ids:
        raise ValueError(f"Duplicate rule id: {rule['id']}")

    if 'kind' not in rule or rule['kind'] not in ('exact', 'regex', 'pattern', 'selector'):
        raise ValueError(f"Rule {rule['id']}: 'kind' must be 'exact', 'regex', 'pattern', or 'selector'")

    # Validate languages
    languages = rule.get('languages', ['python', 'javascript', 'cpp'])
    if not isinstance(languages, list):
        raise ValueError(f"Rule {rule['id']}: 'languages' must be an array")
    for lang in languages:
        if lang not in ('python', 'javascript', 'cpp'):
            raise ValueError(f"Rule {rule['id']}: 'languages' may only contain 'python', 'javascript', or 'cpp', got '{lang}'")

    # Kind-specific validation
    if rule['kind'] in ('exact', 'regex'):
        if 'pattern' not in rule or not isinstance(rule['pattern'], str) or not rule['pattern']:
            raise ValueError(f"Rule {rule['id']}: 'pattern' must be a non-empty string")

    if rule['kind'] == 'regex':
        regex_flags = rule.get('regex_flags', [])
        if not isinstance(regex_flags, list):
            raise ValueError(f"Rule {rule['id']}: 'regex_flags' must be an array")
        allowed_flags = {'i', 'm', 's'}
        for flag in regex_flags:
            if flag not in allowed_flags:
                raise ValueError(f"Rule {rule['id']}: Invalid regex flag '{flag}', allowed: i, m, s")

        flags = get_regex_flags(regex_flags)
        try:
            re.compile(rule['pattern'], flags)
        except re.error as e:
            raise ValueError(f"Rule {rule['id']}: Invalid regex pattern: {e}")

    if rule['kind'] == 'pattern':
        if 'pattern' not in rule or not isinstance(rule['pattern'], str) or not rule['pattern']:
            raise ValueError(f"Rule {rule['id']}: 'pattern' must be a non-empty string")

    if rule['kind'] == 'selector':
        if 'selector' not in rule or not isinstance(rule['selector'], str) or not rule['selector']:
            raise ValueError(f"Rule {rule['id']}: 'selector' must be a non-empty string")
        if rule['selector'] not in ALLOWED_SELECTORS:
            raise ValueError(f"Rule {rule['id']}: Invalid selector '{rule['selector']}'")

    # Validate fix object if present
    if 'fix' in rule:
        fix = rule['fix']
        if not isinstance(fix, dict):
            raise ValueError(f"Rule {rule['id']}: 'fix' must be an object")
        if fix.get('kind') != 'replace':
            raise ValueError(f"Rule {rule['id']}: fix 'kind' must be 'replace'")
        if 'template' not in fix:
            raise ValueError(f"Rule {rule['id']}: 'fix.template' is required")
        if not isinstance(fix['template'], str):
            raise ValueError(f"Rule {rule['id']}: 'fix.template' must be a string")


def load_rules(rules_path: str) -> List[Dict[str, Any]]:
    """Load and validate rules from JSON file."""
    with open(rules_path, 'r', encoding='utf-8') as f:
        rules = json.load(f)

    if not isinstance(rules, list):
        raise ValueError("Rules file must contain a JSON array")

    rule_ids = set()
    for rule in rules:
        validate_rule(rule, rule_ids)
        rule_ids.add(rule['id'])

    # Set defaults
    for rule in rules:
        if 'languages' not in rule:
            rule['languages'] = ['python', 'javascript', 'cpp']
        if rule['kind'] == 'regex' and 'regex_flags' not in rule:
            rule['regex_flags'] = []

    return rules


def get_regex_flags(flags_list: List[str]) -> int:
    """Convert flag list to re flags."""
    flags = 0
    for flag in flags_list:
        if flag == 'i':
            flags |= re.IGNORECASE
        elif flag == 'm':
            flags |= re.MULTILINE
        elif flag == 's':
            flags |= re.DOTALL
    return flags


def get_parser(language: str):
    """Get tree-sitter parser for a language."""
    lang_module = LANGUAGE_PARSERS.get(language)
    if lang_module is None:
        return None
    parser = tree_sitter.Parser()
    parser.language = tree_sitter.Language(lang_module.language())
    return parser


def get_node_text(node: tree_sitter.Node, content: bytes) -> str:
    """Get text content of a tree-sitter node."""
    return content[node.start_byte:node.end_byte].decode('utf-8')


def byte_offset_to_position(content: str, byte_offset: int) -> Tuple[int, int]:
    """Convert byte offset to line and column (1-indexed)."""
    byte_count = 0
    lines = content.split('\n')

    for line_idx, line in enumerate(lines):
        line_bytes = len(line.encode('utf-8'))
        if byte_count + line_bytes >= byte_offset:
            remaining_bytes = byte_offset - byte_count
            char_pos = 0
            byte_pos = 0
            for char in line:
                char_bytes = len(char.encode('utf-8'))
                if byte_pos + char_bytes > remaining_bytes:
                    break
                byte_pos += char_bytes
                char_pos += 1
            return (line_idx + 1, char_pos + 1)  # 1-indexed

        byte_count += line_bytes + 1  # +1 for newline

    return (len(lines), 1)


def find_exact_matches(content: str, pattern: str) -> List[Tuple[int, int, int, int, str]]:
    """Find all exact text matches in content."""
    matches = []
    start = 0
    while True:
        pos = content.find(pattern, start)
        if pos == -1:
            break

        line_start = content.rfind('\n', 0, pos) + 1
        lines_before = content[:pos].count('\n')
        col = pos - line_start + 1

        end_pos = pos + len(pattern)
        line_end_start = content.rfind('\n', 0, end_pos) + 1
        lines_in_match = content[pos:end_pos].count('\n')

        if lines_in_match == 0:
            end_line = lines_before + 1
            end_col = col + len(pattern)
        else:
            end_line = lines_before + lines_in_match + 1
            end_col = end_pos - line_end_start + 1

        matches.append((lines_before + 1, col, end_line, end_col, pattern))
        start = pos + 1

    return matches


def find_regex_matches(content: str, pattern: str, flags: int) -> List[Tuple[int, int, int, int, str]]:
    """Find all regex matches in content."""
    matches = []

    try:
        compiled = re.compile(pattern, flags)
    except re.error:
        return matches

    for match in compiled.finditer(content):
        pos = match.start()
        match_text = match.group()
        end_pos = match.end()

        line_start = content.rfind('\n', 0, pos) + 1
        lines_before = content[:pos].count('\n')
        col = pos - line_start + 1

        line_end_start = content.rfind('\n', 0, end_pos) + 1
        lines_in_match = content[pos:end_pos].count('\n')

        if lines_in_match == 0:
            end_line = lines_before + 1
            end_col = col + len(match_text)
        else:
            end_line = lines_before + lines_in_match + 1
            end_col = end_pos - line_end_start + 1

        matches.append((lines_before + 1, col, end_line, end_col, match_text))

    return matches


def find_selector_matches(content: str, selector: str, language: str) -> List[Tuple[int, int, int, int, str]]:
    """Find all nodes matching a selector in content."""
    matches = []

    parser = get_parser(language)
    if parser is None:
        return matches

    tree = parser.parse(content.encode('utf-8'))
    root = tree.root_node

    # Get the actual tree-sitter node type for this selector
    node_type = NODE_TYPE_MAPPINGS.get(language, {}).get(selector)
    if node_type is None:
        # Try to match the selector directly
        node_type = selector

    def traverse_node(node: tree_sitter.Node):
        # Check if node type matches
        if node.type == node_type:
            start_line = node.start_point[0] + 1
            start_col = node.start_point[1] + 1
            end_line = node.end_point[0] + 1
            end_col = node.end_point[1] + 1
            match_text = content[node.start_byte:node.end_byte]
            matches.append((start_line, start_col, end_line, end_col, match_text))

        for child in node.children:
            traverse_node(child)

    traverse_node(root)
    return matches


def find_pattern_matches(content: str, pattern: str, language: str) -> List[Tuple[int, int, int, int, str, Dict[str, Any]]]:
    """Find all tree-sitter pattern matches with captures."""
    matches = []

    parser = get_parser(language)
    if parser is None:
        return matches

    tree = parser.parse(content.encode('utf-8'))

    # Parse the pattern to extract captures
    # Pattern format: name($CAPTURE1, $CAPTURE2, ...)
    # We need to convert this to a tree-sitter query

    query_str = convert_pattern_to_query(pattern, language)
    if query_str is None:
        return matches

    try:
        lang_module = LANGUAGE_PARSERS[language]
        query = tree_sitter.Query(tree_sitter.Language(lang_module.language()), query_str)
    except Exception:
        return matches

    # Query.captures returns a list of (node, capture_name) tuples
    # We need to group them into matches
    captures_list = query.captures(tree.root_node)

    # Group captures by their match index
    match_groups = {}
    for i, (node, capture_name) in enumerate(captures_list):
        # Each call_expression match will have its captures listed
        if i not in match_groups:
            match_groups[i] = {'captures': {}, 'node': None}

        if capture_name.startswith('_match_'):
            match_groups[i]['node'] = node
        else:
            if capture_name not in match_groups[i]['captures']:
                match_groups[i]['captures'][capture_name] = []

            start_line = node.start_point[0] + 1
            start_col = node.start_point[1] + 1
            end_line = node.end_point[0] + 1
            end_col = node.end_point[1] + 1
            text = content[node.start_byte:node.end_byte]

            match_groups[i]['captures'][capture_name] = {
                'text': text,
                'ranges': [{
                    'start': {'line': start_line, 'col': start_col},
                    'end': {'line': end_line, 'col': end_col}
                }]
            }

    # Process matches
    processed_matches = set()
    for key, match_data in match_groups.items():
        if match_data['node'] is None:
            continue

        node = match_data['node']
        start_pos = (node.start_point[0], node.start_point[1])

        if start_pos in processed_matches:
            continue
        processed_matches.add(start_pos)

        start_line = node.start_point[0] + 1
        start_col = node.start_point[1] + 1
        end_line = node.end_point[0] + 1
        end_col = node.end_point[1] + 1
        match_text = content[node.start_byte:node.end_byte]

        captures = match_data['captures']
        # Sort captures keys lexicographically
        sorted_captures = dict(sorted(captures.items()))

        matches.append((start_line, start_col, end_line, end_col, match_text, sorted_captures))

    return matches


def convert_pattern_to_query(pattern: str, language: str) -> Optional[str]:
    """Convert a pattern like 'print($ARG)' to a tree-sitter query."""
    # Find all $CAPTURE placeholders
    capture_pattern = r'\$([A-Za-z_][A-Za-z0-9_]*)'

    captures = re.findall(capture_pattern, pattern)

    # Replace $CAPTURE with placeholder
    # We need to match the structure in tree-sitter

    # For simple function calls: name($ARG)
    if language == 'python':
        # Check if it's a function call pattern
        match = re.match(r'^([a-zA-Z_][a-zA-Z0-9_]*)\s*\(\s*(\$[A-Za-z_][A-Za-z0-9_]*)\s*\)$', pattern)
        if match:
            func_name = match.group(1)
            capture_name = match.group(2)[1:]  # Remove $
            return f"""
            (call
              function: (identifier) @func_name (#eq? @func_name "{func_name}")
              arguments: (argument_list
                (_) @{capture_name}
              )
            ) @_match_1
            """

        # Handle function call with multiple args
        match = re.match(r'^([a-zA-Z_][a-zA-Z0-9_]*)\s*\((.*)\)$', pattern)
        if match:
            func_name = match.group(1)
            args_str = match.group(2)

            # Parse arguments with captures
            args = []
            for m in re.finditer(r'\$([A-Za-z_][A-Za-z0-9_]*)', args_str):
                args.append(m.group(1))

            if len(args) == 1:
                return f"""
                (call
                  function: (identifier) @func_name (#eq? @func_name "{func_name}")
                  arguments: (argument_list
                    (_) @{args[0]}
                  )
                ) @_match_1
                """
            else:
                # Multiple arguments
                arg_patterns = "\n".join([f"(_) @{arg}" for arg in args])
                return f"""
                (call
                  function: (identifier) @func_name (#eq? @func_name "{func_name}")
                  arguments: (argument_list
                    {arg_patterns}
                  )
                ) @_match_1
                """

    elif language == 'javascript':
        match = re.match(r'^([a-zA-Z_][a-zA-Z0-9_]*)\s*\(\s*(\$[A-Za-z_][A-Za-z0-9_]*)\s*\)$', pattern)
        if match:
            func_name = match.group(1)
            capture_name = match.group(2)[1:]  # Remove $
            return f"""
            (call_expression
              function: (identifier) @func_name (#eq? @func_name "{func_name}")
              arguments: (arguments
                (_) @{capture_name}
              )
            ) @_match_1
            """

    elif language == 'cpp':
        match = re.match(r'^([a-zA-Z_][a-zA-Z0-9_]*)\s*\(\s*(\$[A-Za-z_][A-Za-z0-9_]*)\s*\)$', pattern)
        if match:
            func_name = match.group(1)
            capture_name = match.group(2)[1:]  # Remove $
            return f"""
            (call_expression
              function: (identifier) @func_name (#eq? @func_name "{func_name}")
              arguments: (argument_list
                (_) @{capture_name}
              )
            ) @_match_1
            """

    return None


def expand_template(template: str, match_text: str, captures: Optional[Dict[str, Any]] = None) -> str:
    """Expand a fix template with placeholders."""
    result = []
    i = 0
    while i < len(template):
        if template[i] == '$':
            if i + 1 < len(template) and template[i + 1] == '$':
                # Escaped $
                result.append('$')
                i += 2
            elif i + 1 < len(template):
                # Find the placeholder name
                j = i + 1
                while j < len(template) and (template[j].isalnum() or template[j] == '_'):
                    j += 1
                name = template[i + 1:j]

                if name == 'MATCH':
                    result.append(match_text)
                elif captures and name in captures:
                    # Get the first occurrence's text
                    cap_data = captures[name]
                    if isinstance(cap_data, dict) and 'text' in cap_data:
                        result.append(cap_data['text'])
                    elif isinstance(cap_data, list) and len(cap_data) > 0:
                        result.append(cap_data[0].get('text', ''))
                else:
                    # Unknown placeholder, keep as-is
                    result.append(template[i:j])
                i = j
            else:
                result.append('$')
                i += 1
        else:
            result.append(template[i])
            i += 1

    return ''.join(result)


def check_overlap(fix1: Dict[str, Any], fix2: Dict[str, Any]) -> bool:
    """Check if two fix ranges overlap."""
    if fix1['file'] != fix2['file']:
        return False

    # Convert to byte offsets for comparison
    # Range 1: (start_line, start_col) to (end_line, end_col)
    # Range 2: (start_line, start_col) to (end_line, end_col)

    s1 = (fix1['start']['line'], fix1['start']['col'])
    e1 = (fix1['end']['line'], fix1['end']['col'])
    s2 = (fix2['start']['line'], fix2['start']['col'])
    e2 = (fix2['end']['line'], fix2['end']['col'])

    # Ranges overlap if one doesn't end before the other starts
    return not (e1 <= s2 or e2 <= s1)


def search_file(file_path: Path, root_dir: Path, rules: List[Dict[str, Any]], encoding: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Search a single file for matches and generate fix candidates."""
    matches = []
    fix_candidates = []

    # Determine language from extension
    ext = file_path.suffix.lower()
    language = LANGUAGE_EXTENSIONS.get(ext)
    if language is None:
        return matches, fix_candidates

    # Check if any rule applies to this language
    applicable_rules = [r for r in rules if language in r.get('languages', ['python', 'javascript', 'cpp'])]
    if not applicable_rules:
        return matches, fix_candidates

    try:
        with open(file_path, 'r', encoding=encoding) as f:
            content = f.read()
    except (UnicodeDecodeError, IOError):
        return matches, fix_candidates

    rel_path = file_path.relative_to(root_dir).as_posix()

    for rule in applicable_rules:
        if rule['kind'] == 'exact':
            found = find_exact_matches(content, rule['pattern'])
            for start_line, start_col, end_line, end_col, match_text in found:
                match_obj = {
                    'rule_id': rule['id'],
                    'file': rel_path,
                    'language': language,
                    'start': {'line': start_line, 'col': start_col},
                    'end': {'line': end_line, 'col': end_col},
                    'match': match_text
                }
                matches.append(match_obj)

                # Generate fix candidate if rule has fix
                if 'fix' in rule:
                    replacement = expand_template(rule['fix']['template'], match_text)
                    fix_obj = {
                        'rule_id': rule['id'],
                        'file': rel_path,
                        'language': language,
                        'start': {'line': start_line, 'col': start_col},
                        'end': {'line': end_line, 'col': end_col},
                        'replacement': replacement,
                        'match_text': match_text
                    }
                    fix_candidates.append(fix_obj)

        elif rule['kind'] == 'regex':
            flags = get_regex_flags(rule.get('regex_flags', []))
            found = find_regex_matches(content, rule['pattern'], flags)
            for start_line, start_col, end_line, end_col, match_text in found:
                match_obj = {
                    'rule_id': rule['id'],
                    'file': rel_path,
                    'language': language,
                    'start': {'line': start_line, 'col': start_col},
                    'end': {'line': end_line, 'col': end_col},
                    'match': match_text
                }
                matches.append(match_obj)

                # Generate fix candidate if rule has fix
                if 'fix' in rule:
                    replacement = expand_template(rule['fix']['template'], match_text)
                    fix_obj = {
                        'rule_id': rule['id'],
                        'file': rel_path,
                        'language': language,
                        'start': {'line': start_line, 'col': start_col},
                        'end': {'line': end_line, 'col': end_col},
                        'replacement': replacement,
                        'match_text': match_text
                    }
                    fix_candidates.append(fix_obj)

        elif rule['kind'] == 'selector':
            found = find_selector_matches(content, rule['selector'], language)
            for start_line, start_col, end_line, end_col, match_text in found:
                match_obj = {
                    'rule_id': rule['id'],
                    'file': rel_path,
                    'language': language,
                    'start': {'line': start_line, 'col': start_col},
                    'end': {'line': end_line, 'col': end_col},
                    'match': match_text
                }
                matches.append(match_obj)

                # Generate fix candidate if rule has fix
                if 'fix' in rule:
                    replacement = expand_template(rule['fix']['template'], match_text)
                    fix_obj = {
                        'rule_id': rule['id'],
                        'file': rel_path,
                        'language': language,
                        'start': {'line': start_line, 'col': start_col},
                        'end': {'line': end_line, 'col': end_col},
                        'replacement': replacement,
                        'match_text': match_text
                    }
                    fix_candidates.append(fix_obj)

        elif rule['kind'] == 'pattern':
            found = find_pattern_matches(content, rule['pattern'], language)
            for start_line, start_col, end_line, end_col, match_text, captures in found:
                match_obj = {
                    'rule_id': rule['id'],
                    'file': rel_path,
                    'language': language,
                    'start': {'line': start_line, 'col': start_col},
                    'end': {'line': end_line, 'col': end_col},
                    'match': match_text,
                    'captures': captures
                }
                matches.append(match_obj)

                # Generate fix candidate if rule has fix
                if 'fix' in rule:
                    replacement = expand_template(rule['fix']['template'], match_text, captures)
                    fix_obj = {
                        'rule_id': rule['id'],
                        'file': rel_path,
                        'language': language,
                        'start': {'line': start_line, 'col': start_col},
                        'end': {'line': end_line, 'col': end_col},
                        'replacement': replacement,
                        'match_text': match_text
                    }
                    fix_candidates.append(fix_obj)

    return matches, fix_candidates


def search_directory(root_dir: Path, rules: List[Dict[str, Any]], encoding: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Search directory recursively for matches."""
    all_matches = []
    all_fix_candidates = []

    for dirpath, dirnames, filenames in os.walk(root_dir):
        for filename in filenames:
            ext = Path(filename).suffix.lower()
            if ext in LANGUAGE_EXTENSIONS:
                file_path = Path(dirpath) / filename
                matches, fix_candidates = search_file(file_path, root_dir, rules, encoding)
                all_matches.extend(matches)
                all_fix_candidates.extend(fix_candidates)

    return all_matches, all_fix_candidates


def apply_fixes_to_file(file_path: Path, fixes: List[Dict[str, Any]], encoding: str) -> None:
    """Apply fixes to a single file."""
    if not fixes:
        return

    try:
        with open(file_path, 'r', encoding=encoding) as f:
            content = f.read()
    except (UnicodeDecodeError, IOError):
        return

    # Sort fixes in reverse order (by position) to apply from end to start
    sorted_fixes = sorted(fixes, key=lambda f: (
        -f['start']['line'],
        -f['start']['col']
    ))

    lines = content.split('\n')

    for fix in sorted_fixes:
        start_line = fix['start']['line'] - 1  # 0-indexed
        start_col = fix['start']['col'] - 1  # 0-indexed
        end_line = fix['end']['line'] - 1  # 0-indexed
        end_col = fix['end']['col'] - 1  # 0-indexed

        # Build new content
        if start_line == end_line:
            # Single line replacement
            line = lines[start_line]
            new_line = line[:start_col] + fix['replacement'] + line[end_col:]
            lines[start_line] = new_line
        else:
            # Multi-line replacement
            first_line = lines[start_line][:start_col]
            last_line = lines[end_line][end_col:]
            lines[start_line] = first_line + fix['replacement'] + last_line
            # Remove the lines in between
            del lines[start_line + 1:end_line + 1]

    new_content = '\n'.join(lines)

    with open(file_path, 'w', encoding=encoding) as f:
        f.write(new_content)


def sort_key(item: Dict[str, Any]) -> Tuple:
    """Generate sort key for output items."""
    return (
        item['file'],
        item['start']['line'],
        item['start']['col'],
        item['rule_id'],
        0 if 'event' not in item else 1  # match before fix
    )


def main():
    """Main entry point."""
    args = parse_args()

    root_dir = Path(args.root_dir).resolve()
    if not root_dir.is_dir():
        print(f"Error: {args.root_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    try:
        rules = load_rules(args.rules)
    except (json.JSONDecodeError, ValueError, IOError) as e:
        print(f"Error loading rules: {e}", file=sys.stderr)
        sys.exit(1)

    matches, fix_candidates = search_directory(root_dir, rules, args.encoding)

    # Sort matches
    matches.sort(key=sort_key)

    # Determine mode
    dry_run = args.dry_run
    apply_fixes = args.apply_fixes

    if dry_run or apply_fixes:
        # Sort fix candidates by file, then position, then rule_id
        fix_candidates.sort(key=lambda f: (f['file'], f['start']['line'], f['start']['col'], f['rule_id']))

        # Conflict resolution: skip overlapping fixes
        applied_fixes = []
        skipped_fixes = []
        applied_ranges = {}  # file -> list of (start, end) tuples

        for fix in fix_candidates:
            file_path = fix['file']
            if file_path not in applied_ranges:
                applied_ranges[file_path] = []

            # Check for overlap with previously applied fixes
            has_overlap = False
            for applied in applied_ranges[file_path]:
                if check_overlap(fix, applied):
                    has_overlap = True
                    break

            if has_overlap:
                fix['applied'] = False
                fix['skipped_reason'] = 'overlap'
                skipped_fixes.append(fix)
            else:
                fix['applied'] = True
                fix['skipped_reason'] = None
                applied_fixes.append(fix)
                applied_ranges[file_path].append(fix)

        # Build output lines
        output_lines = []

        # Add match lines
        for match in matches:
            output_lines.append(match)

        # Add fix lines
        all_fixes = applied_fixes + skipped_fixes
        all_fixes.sort(key=lambda f: (f['file'], f['start']['line'], f['start']['col'], f['rule_id']))

        for fix in all_fixes:
            fix_line = {
                'event': 'fix',
                'rule_id': fix['rule_id'],
                'file': fix['file'],
                'language': fix['language'],
                'start': fix['start'],
                'end': fix['end'],
                'replacement': fix['replacement'],
                'applied': fix['applied'],
                'skipped_reason': fix['skipped_reason']
            }
            output_lines.append(fix_line)

        # Sort all output lines
        output_lines.sort(key=sort_key)

        # Print output
        for line in output_lines:
            print(json.dumps(line))

        # Apply fixes to files if --apply-fixes mode
        if apply_fixes:
            # Group fixes by file
            fixes_by_file = {}
            for fix in applied_fixes:
                file_path = fix['file']
                if file_path not in fixes_by_file:
                    fixes_by_file[file_path] = []
                fixes_by_file[file_path].append(fix)

            # Apply fixes to each file
            for rel_path, fixes in fixes_by_file.items():
                file_path = root_dir / rel_path
                apply_fixes_to_file(file_path, fixes, args.encoding)
    else:
        # No fix mode - just print matches
        for match in matches:
            print(json.dumps(match))


if __name__ == '__main__':
    main()
