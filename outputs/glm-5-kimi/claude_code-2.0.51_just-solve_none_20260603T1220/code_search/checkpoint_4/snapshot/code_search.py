#!/usr/bin/env python3
"""
Command-line code searcher for Python, JavaScript, and C++ codebases.
Searches for exact matches, regex patterns, and structure-aware patterns in source files.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple, Set

# Add local packages path if available
sys.path.insert(0, '/workspace/.python_packages')

import tree_sitter_python as tspython
import tree_sitter_javascript as tsjs
import tree_sitter_cpp as tscpp
from tree_sitter import Language, Parser, Node


# Allowed selector node types
ALLOWED_SELECTORS = {
    'program', 'module', 'namespace', 'import', 'export', 'declaration', 'definition',
    'variable_declaration', 'constant_declaration', 'type_declaration', 'function_declaration',
    'method_declaration', 'class_declaration', 'interface_declaration', 'struct_declaration',
    'enum_declaration', 'field_declaration', 'type', 'generic_type', 'parameter', 'argument',
    'block', 'statement', 'return_statement', 'throw_statement', 'break_statement',
    'continue_statement', 'if_statement', 'else_clause', 'switch_statement', 'case_clause',
    'for_statement', 'while_statement', 'do_while_statement', 'try_statement', 'catch_clause',
    'finally_clause', 'expression', 'assignment_expression', 'call_expression',
    'member_expression', 'subscript_expression', 'binary_expression', 'unary_expression',
    'conditional_expression', 'literal', 'string_literal', 'numeric_literal',
    'boolean_literal', 'null_literal', 'array_literal', 'object_literal', 'annotation',
    'decorator', 'attribute', 'access_modifier', 'operator', 'identifier', 'comment'
}


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description='Code searcher for Python, JavaScript, and C++ codebases')
    parser.add_argument('root_dir', help='Path to the codebase to scan')
    parser.add_argument('--rules', required=True, help='Path to JSON rules file')
    parser.add_argument('--encoding', default='utf-8', help='File encoding (default: utf-8)')

    # Fix mode arguments (mutually exclusive)
    fix_group = parser.add_mutually_exclusive_group()
    fix_group.add_argument('--dry-run', action='store_true',
                           help='Preview fixes without writing to disk')
    fix_group.add_argument('--apply-fixes', action='store_true',
                           help='Apply fixes and write changes to disk')

    return parser.parse_args()


# Language parsers (lazy loaded)
_PARSERS: Dict[str, Parser] = {}
_LANGUAGES: Dict[str, Language] = {}


def get_parser(language: str) -> Parser:
    """Get or create a parser for the given language."""
    if language not in _PARSERS:
        if language == 'python':
            lang = Language(tspython.language())
        elif language == 'javascript':
            lang = Language(tsjs.language())
        elif language == 'cpp':
            lang = Language(tscpp.language())
        else:
            raise ValueError(f"Unknown language: {language}")
        _LANGUAGES[language] = lang
        _PARSERS[language] = Parser(lang)
    return _PARSERS[language]


def get_language(language: str) -> Language:
    """Get the Language object for the given language."""
    if language not in _LANGUAGES:
        get_parser(language)  # This will populate _LANGUAGES
    return _LANGUAGES[language]


def load_rules(rules_path: str) -> List[Dict[str, Any]]:
    """Load and validate rules from JSON file."""
    with open(rules_path, 'r', encoding='utf-8') as f:
        rules = json.load(f)

    if not isinstance(rules, list):
        raise ValueError("Rules file must contain a JSON array")

    validated_rules = []
    seen_ids = set()

    for i, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise ValueError(f"Rule {i} must be an object")

        # Validate id
        if 'id' not in rule or not isinstance(rule['id'], str) or not rule['id']:
            raise ValueError(f"Rule {i} must have a non-empty string 'id'")

        if rule['id'] in seen_ids:
            raise ValueError(f"Duplicate rule id: {rule['id']}")
        seen_ids.add(rule['id'])

        # Validate kind
        valid_kinds = ('exact', 'regex', 'pattern', 'selector')
        if 'kind' not in rule or rule['kind'] not in valid_kinds:
            raise ValueError(f"Rule {i} must have 'kind' of 'exact', 'regex', 'pattern', or 'selector'")

        # Validate languages (optional)
        valid_languages = {'python', 'javascript', 'cpp'}
        languages = rule.get('languages', list(valid_languages))
        if not isinstance(languages, list) or not languages:
            raise ValueError(f"Rule {i} 'languages' must be a non-empty array")
        for lang in languages:
            if lang not in valid_languages:
                raise ValueError(f"Rule {i} 'languages' may only contain 'python', 'javascript', 'cpp', found: {lang}")

        validated_rule = {
            'id': rule['id'],
            'kind': rule['kind'],
            'languages': languages,
        }

        # Validate pattern (required for exact, regex, pattern kinds)
        if rule['kind'] in ('exact', 'regex', 'pattern'):
            if 'pattern' not in rule or not isinstance(rule['pattern'], str) or not rule['pattern']:
                raise ValueError(f"Rule {i} must have a non-empty string 'pattern'")
            validated_rule['pattern'] = rule['pattern']

        # Validate selector (required for selector kind)
        if rule['kind'] == 'selector':
            if 'selector' not in rule or not isinstance(rule['selector'], str) or not rule['selector']:
                raise ValueError(f"Rule {i} must have a non-empty string 'selector'")
            if rule['selector'] not in ALLOWED_SELECTORS:
                raise ValueError(f"Rule {i} has invalid selector: {rule['selector']}")
            validated_rule['selector'] = rule['selector']

        # Validate regex_flags (optional, only for regex)
        regex_flags = rule.get('regex_flags', [])
        if rule['kind'] == 'regex':
            if not isinstance(regex_flags, list):
                raise ValueError(f"Rule {i} 'regex_flags' must be an array")
            for flag in regex_flags:
                if flag not in ('i', 'm', 's'):
                    raise ValueError(f"Rule {i} 'regex_flags' may only contain 'i', 'm', 's', found: {flag}")
        validated_rule['regex_flags'] = regex_flags if rule['kind'] == 'regex' else []

        # Validate fix (optional)
        if 'fix' in rule:
            fix = rule['fix']
            if not isinstance(fix, dict):
                raise ValueError(f"Rule {i} 'fix' must be an object")
            if 'kind' not in fix or fix['kind'] != 'replace':
                raise ValueError(f"Rule {i} 'fix.kind' must be 'replace'")
            if 'template' not in fix or not isinstance(fix['template'], str):
                raise ValueError(f"Rule {i} 'fix.template' must be a string")
            validated_rule['fix'] = {
                'kind': 'replace',
                'template': fix['template']
            }

        # Pre-compile regex patterns
        if validated_rule['kind'] == 'regex':
            flags = 0
            for flag in validated_rule['regex_flags']:
                if flag == 'i':
                    flags |= re.IGNORECASE
                elif flag == 'm':
                    flags |= re.MULTILINE
                elif flag == 's':
                    flags |= re.DOTALL
            try:
                validated_rule['compiled_pattern'] = re.compile(validated_rule['pattern'], flags)
            except re.error as e:
                raise ValueError(f"Rule {i} has invalid regex pattern: {e}")

        # Extract metavariables for pattern rules
        if validated_rule['kind'] == 'pattern':
            validated_rule['metavariables'] = extract_metavariables(validated_rule['pattern'])

        validated_rules.append(validated_rule)

    return validated_rules


def extract_metavariables(pattern: str) -> Dict[str, bool]:
    """
    Extract metavariables from a pattern string.
    Returns a dict mapping metavariable name to whether it's optional (ends with ?).
    """
    meta_vars = {}
    i = 0
    while i < len(pattern):
        # Check for $$ (escaped $)
        if i + 1 < len(pattern) and pattern[i:i+2] == '$$':
            i += 2
            continue
        # Check for $NAME or $NAME?
        if pattern[i] == '$':
            # Find the end of the metavariable name
            j = i + 1
            while j < len(pattern) and (pattern[j].isalnum() or pattern[j] == '_'):
                j += 1
            # Check for optional marker
            is_optional = False
            if j < len(pattern) and pattern[j] == '?':
                is_optional = True
                j += 1
            if j > i + 1:  # We found a valid metavariable name
                name = pattern[i:j] if is_optional else pattern[i:j]
                meta_vars[name] = is_optional
            i = j
        else:
            i += 1
    return meta_vars


def create_pattern_template(pattern: str) -> Tuple[str, Dict[str, str]]:
    """
    Create a template string by replacing metavariables with valid identifiers.
    Returns the template string and a mapping from placeholder to original metavariable.
    """
    placeholder_map = {}
    result = []
    i = 0
    placeholder_counter = 0

    while i < len(pattern):
        # Check for $$ (escaped $)
        if i + 1 < len(pattern) and pattern[i:i+2] == '$$':
            result.append('$')
            i += 2
            continue
        # Check for $NAME or $NAME?
        if pattern[i] == '$':
            j = i + 1
            while j < len(pattern) and (pattern[j].isalnum() or pattern[j] == '_'):
                j += 1
            # Check for optional marker
            is_optional = False
            if j < len(pattern) and pattern[j] == '?':
                is_optional = True
                j += 1
            if j > i + 1:  # We found a valid metavariable
                var_name = pattern[i:j] if is_optional else pattern[i:j]
                # Create a valid identifier placeholder
                placeholder = f"MVAR_{placeholder_counter}"
                placeholder_counter += 1
                placeholder_map[placeholder] = var_name
                result.append(placeholder)
                i = j
            else:
                result.append(pattern[i])
                i += 1
        else:
            result.append(pattern[i])
            i += 1

    return ''.join(result), placeholder_map


def parse_code(code: bytes, language: str) -> Optional[Node]:
    """Parse code and return the root node."""
    parser = get_parser(language)
    tree = parser.parse(code)
    return tree.root_node


def get_node_text(node: Node, source: bytes) -> str:
    """Get the text content of a node."""
    return node.text.decode('utf-8', errors='replace')


def count_leading_tabs(text: str) -> int:
    """Count leading tabs in a string."""
    count = 0
    for c in text:
        if c == '\t':
            count += 1
        else:
            break
    return count


def normalize_whitespace(text: str) -> str:
    """Normalize whitespace for comparison, preserving relative structure."""
    # Split into lines and normalize each line's leading whitespace
    lines = text.split('\n')
    if len(lines) == 1:
        return text.strip()

    # Find minimum indentation (excluding empty lines)
    min_indent = float('inf')
    for line in lines[1:]:  # Skip first line as it may have different indent
        stripped = line.lstrip()
        if stripped:
            indent = len(line) - len(stripped)
            min_indent = min(min_indent, indent)

    if min_indent == float('inf'):
        min_indent = 0

    # Normalize
    result = [lines[0].strip()]
    for line in lines[1:]:
        stripped = line.strip()
        if stripped:
            # Remove the minimum indent
            if len(line) >= min_indent:
                result.append(line[min_indent:].rstrip())
            else:
                result.append(stripped)

    return '\n'.join(result)


def build_pattern_tree(pattern: str, language: str) -> Optional[Node]:
    """Build a tree-sitter AST from the pattern string."""
    template, placeholder_map = create_pattern_template(pattern)

    parser = get_parser(language)
    tree = parser.parse(template.encode('utf-8'))
    return tree.root_node, template, placeholder_map


def extract_meaningful_node(root: Node) -> Node:
    """
    Extract the inner meaningful node from a pattern tree.
    This skips wrapper nodes like 'module' and 'expression_statement'.
    """
    node = root
    # Skip wrapper nodes - module, expression_statement, etc.
    while True:
        if node.type in ('module', 'expression_statement', 'program'):
            if len(node.children) == 1:
                node = node.children[0]
            else:
                break
        else:
            break
    return node


def compare_nodes(pattern_node: Node, source_node: Node, source_bytes: bytes,
                  captures: Dict[str, List[Tuple[int, int]]],
                  metavar_map: Dict[str, str], placeholder_template: str) -> bool:
    """
    Compare a pattern node with a source node, collecting captures.
    Returns True if they match.
    """
    # Check if this is a placeholder/metavariable node
    pattern_text = pattern_node.text.decode('utf-8')

    # Check if pattern text matches a placeholder
    if pattern_text.startswith('MVAR_') and pattern_text in metavar_map:
        meta_var = metavar_map[pattern_text]

        # Get the matched text from source
        source_text = get_node_text(source_node, source_bytes)

        # Check if this metavariable was already bound
        if meta_var in captures:
            # Must match exactly the same text - we need to get the actual text from first capture
            first_range = captures[meta_var][0]
            existing_text = source_bytes[first_range[0]:first_range[1]].decode('utf-8')
            if source_text != existing_text:
                return False
            # Add another range for this occurrence
            captures[meta_var].append((source_node.start_byte, source_node.end_byte))
            return True
        else:
            # First occurrence - bind it
            captures[meta_var] = [(source_node.start_byte, source_node.end_byte)]
            return True

    # For non-placeholder nodes, compare structure
    # Node types must match
    if pattern_node.type != source_node.type:
        # Special case: ERROR nodes in pattern should match anything
        if pattern_node.type == 'ERROR':
            return False
        return False

    # For leaf nodes, compare text
    if len(pattern_node.children) == 0:
        pattern_text = pattern_node.text.decode('utf-8')
        source_text = get_node_text(source_node, source_bytes)
        return pattern_text == source_text

    # For internal nodes, recursively compare children
    if len(pattern_node.children) != len(source_node.children):
        return False

    for p_child, s_child in zip(pattern_node.children, source_node.children):
        if not compare_nodes(p_child, s_child, source_bytes, captures, metavar_map, placeholder_template):
            return False

    return True


def find_matches_in_tree(root: Node, pattern_root: Node, source_bytes: bytes,
                          metavar_map: Dict[str, str], placeholder_template: str,
                          optional_vars: Set[str]) -> List[Dict[str, Any]]:
    """Find all matches of the pattern tree in the source tree."""
    matches = []

    def visit(node: Node):
        captures: Dict[str, List[Tuple[int, int]]] = {}
        if compare_nodes(pattern_root, node, source_bytes, captures, metavar_map, placeholder_template):
            # Check that all non-optional metavariables are bound
            all_bound = True
            for var_name in metavar_map.values():
                if var_name not in optional_vars and var_name not in captures:
                    all_bound = False
                    break

            if all_bound:
                matches.append({
                    'node': node,
                    'captures': captures.copy()
                })

        # Recursively visit children
        for child in node.children:
            visit(child)

    visit(root)
    return matches


def search_pattern(rule: Dict[str, Any], content: str, language: str,
                   file_path: str) -> List[Dict[str, Any]]:
    """Search for pattern matches in content."""
    matches = []

    pattern = rule['pattern']
    metavariables = rule.get('metavariables', {})
    optional_vars = {name for name, is_optional in metavariables.items() if is_optional}

    # Build pattern tree
    pattern_root, template, placeholder_map = build_pattern_tree(pattern, language)

    if pattern_root is None or pattern_root.has_error:
        # If pattern has parse errors, skip this rule
        return matches

    # Extract the meaningful node from the pattern tree (skip wrappers)
    pattern_node = extract_meaningful_node(pattern_root)

    # Parse source code
    source_bytes = content.encode('utf-8')
    source_root = parse_code(source_bytes, language)

    if source_root is None:
        return matches

    # Find matches using the extracted pattern node
    raw_matches = find_matches_in_tree(source_root, pattern_node, source_bytes,
                                        placeholder_map, template, optional_vars)

    for match in raw_matches:
        node = match['node']
        captures = match['captures']

        start_line, start_col = byte_to_line_col(content, node.start_byte)
        end_line, end_col = byte_to_line_col(content, node.end_byte)

        matched_text = content[node.start_byte:node.end_byte]

        # Build captures dict with sorted keys
        captures_dict = {}
        for var_name in sorted(captures.keys()):
            ranges = captures[var_name]
            text = content[ranges[0][0]:ranges[0][1]]
            range_list = []
            for start_byte, end_byte in ranges:
                r_start_line, r_start_col = byte_to_line_col(content, start_byte)
                r_end_line, r_end_col = byte_to_line_col(content, end_byte)
                range_list.append({
                    'start': {'line': r_start_line, 'col': r_start_col},
                    'end': {'line': r_end_line, 'col': r_end_col}
                })
            captures_dict[var_name] = {
                'text': text,
                'ranges': range_list
            }

        match_obj = {
            'rule_id': rule['id'],
            'file': file_path,
            'language': language,
            'start': {'line': start_line, 'col': start_col},
            'end': {'line': end_line, 'col': end_col},
            'match': matched_text,
            'captures': captures_dict
        }
        matches.append(match_obj)

    return matches


def byte_to_line_col(content: str, byte_pos: int) -> Tuple[int, int]:
    """Convert byte position to 1-based line and column.

    For start positions, returns the 1-based column of the first character.
    For end positions (exclusive byte_pos), returns the 1-based column after the last character.
    """
    line = 1
    col = 1

    for i, c in enumerate(content):
        if i >= byte_pos:
            break
        if c == '\n':
            line += 1
            col = 1
        else:
            col += 1

    return line, col


# Mapping of file extensions to language names
EXTENSION_TO_LANGUAGE = {
    '.py': 'python',
    '.js': 'javascript',
    '.mjs': 'javascript',
    '.cjs': 'javascript',
    '.cc': 'cpp',
    '.cpp': 'cpp',
    '.cxx': 'cpp',
    '.hh': 'cpp',
    '.hpp': 'cpp',
    '.hxx': 'cpp',
}


def find_source_files(root_dir: str) -> List[Tuple[Path, str]]:
    """Find all source files under root_dir recursively.

    Returns a list of (file_path, language) tuples.
    """
    source_files = []
    root_path = Path(root_dir)

    for path in root_path.rglob('*'):
        if path.is_file():
            ext = path.suffix.lower()
            if ext in EXTENSION_TO_LANGUAGE:
                source_files.append((path, EXTENSION_TO_LANGUAGE[ext]))

    return sorted(source_files, key=lambda x: x[0])


def get_relative_path(file_path: Path, root_dir: str) -> str:
    """Get relative path from root_dir with forward slashes."""
    rel_path = file_path.relative_to(root_dir)
    return str(rel_path).replace(os.sep, '/')


def find_all_positions(text: str, pattern: str) -> List[int]:
    """Find all positions of exact pattern in text."""
    positions = []
    start = 0
    while True:
        pos = text.find(pattern, start)
        if pos == -1:
            break
        positions.append(pos)
        start = pos + 1
    return positions


def search_selector(rule: Dict[str, Any], content: str, language: str,
                    file_path: str) -> List[Dict[str, Any]]:
    """Search for selector matches in content."""
    matches = []

    selector = rule['selector']

    # Parse source code
    source_bytes = content.encode('utf-8')
    source_root = parse_code(source_bytes, language)

    if source_root is None:
        return matches

    def visit(node: Node):
        # Check if node type matches the selector
        if node.type == selector:
            start_line, start_col = byte_to_line_col(content, node.start_byte)
            end_line, end_col = byte_to_line_col(content, node.end_byte)
            matched_text = content[node.start_byte:node.end_byte]

            match_obj = {
                'rule_id': rule['id'],
                'file': file_path,
                'language': language,
                'start': {'line': start_line, 'col': start_col},
                'end': {'line': end_line, 'col': end_col},
                'match': matched_text
            }
            matches.append(match_obj)

        # Recursively visit children
        for child in node.children:
            visit(child)

    visit(source_root)
    return matches


def expand_template(template: str, match_text: str, captures: Optional[Dict[str, Any]] = None) -> str:
    """
    Expand a template string with placeholders.

    $NAME is replaced by the capture's text (first occurrence).
    $MATCH is replaced by the full matched text.
    $$ is replaced by a literal $.
    """
    result = []
    i = 0

    while i < len(template):
        # Check for $$ (escaped $)
        if i + 1 < len(template) and template[i:i+2] == '$$':
            result.append('$')
            i += 2
            continue

        # Check for $MATCH first (special placeholder)
        if template[i:i+6] == '$MATCH':
            result.append(match_text)
            i += 6
            continue

        # Check for $NAME (capture variable)
        if template[i] == '$':
            j = i + 1
            while j < len(template) and (template[j].isalnum() or template[j] == '_'):
                j += 1
            if j > i + 1:  # We found a valid variable name
                var_name = template[i+1:j]
                # Look up in captures - note: captures keys include $ prefix
                if captures:
                    capture_key = '$' + var_name
                    if capture_key in captures:
                        result.append(captures[capture_key]['text'])
                    elif var_name in captures:
                        result.append(captures[var_name]['text'])
                    else:
                        # Variable not found, leave as-is
                        result.append(template[i:j])
                else:
                    # No captures, leave as-is
                    result.append(template[i:j])
                i = j
            else:
                result.append(template[i])
                i += 1
        else:
            result.append(template[i])
            i += 1

    return ''.join(result)


def search_file(file_path: Path, language: str, root_dir: str,
                rules: List[Dict[str, Any]], encoding: str) -> List[Dict[str, Any]]:
    """Search a single file for all rule matches."""
    matches = []

    # Filter rules that apply to this language
    applicable_rules = [r for r in rules if language in r['languages']]

    if not applicable_rules:
        return matches

    try:
        with open(file_path, 'r', encoding=encoding) as f:
            content = f.read()
    except (UnicodeDecodeError, OSError):
        # Skip files that fail to decode
        return matches

    relative_path = get_relative_path(file_path, root_dir)

    for rule in applicable_rules:
        if rule['kind'] == 'exact':
            positions = find_all_positions(content, rule['pattern'])
            for pos in positions:
                start_line, start_col = byte_to_line_col(content, pos)
                end_line, end_col = byte_to_line_col(content, pos + len(rule['pattern']))

                match_obj = {
                    'rule_id': rule['id'],
                    'file': relative_path,
                    'language': language,
                    'start': {'line': start_line, 'col': start_col},
                    'end': {'line': end_line, 'col': end_col},
                    'match': rule['pattern']
                }
                matches.append(match_obj)

        elif rule['kind'] == 'regex':
            compiled = rule['compiled_pattern']
            for match in compiled.finditer(content):
                start_line, start_col = byte_to_line_col(content, match.start())
                end_line, end_col = byte_to_line_col(content, match.end())

                match_obj = {
                    'rule_id': rule['id'],
                    'file': relative_path,
                    'language': language,
                    'start': {'line': start_line, 'col': start_col},
                    'end': {'line': end_line, 'col': end_col},
                    'match': match.group()
                }
                matches.append(match_obj)

        elif rule['kind'] == 'pattern':
            pattern_matches = search_pattern(rule, content, language, relative_path)
            matches.extend(pattern_matches)

        elif rule['kind'] == 'selector':
            selector_matches = search_selector(rule, content, language, relative_path)
            matches.extend(selector_matches)

    return matches


def sort_matches(matches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Sort matches by file, start position, end position, then rule_id."""
    def sort_key(m):
        return (
            m['file'],
            m['start']['line'],
            m['start']['col'],
            m['end']['line'],
            m['end']['col'],
            m['rule_id']
        )

    return sorted(matches, key=sort_key)


def ranges_overlap(start1: int, end1: int, start2: int, end2: int) -> bool:
    """Check if two byte ranges overlap."""
    return not (end1 <= start2 or end2 <= start1)


def apply_fixes(file_path: Path, matches: List[Dict[str, Any]],
                encoding: str, dry_run: bool) -> List[Dict[str, Any]]:
    """
    Process and apply fixes to a file.

    Returns a list of fix result objects.
    """
    fix_results = []

    # Collect all matches with fixes, sorted by position
    matches_with_fixes = []
    for match in matches:
        rule_id = match['rule_id']
        # Find the rule to check if it has a fix
        # We'll need to pass rules separately, but for now let's use the match itself
        matches_with_fixes.append(match)

    # Sort matches by position (file is already handled at higher level)
    sorted_matches = sorted(matches_with_fixes, key=lambda m: (
        m['start']['line'],
        m['start']['col'],
        m['rule_id']
    ))

    return fix_results


def main():
    args = parse_args()

    # Validate root_dir exists
    if not os.path.isdir(args.root_dir):
        print(f"Error: {args.root_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    # Validate rules file exists
    if not os.path.isfile(args.rules):
        print(f"Error: {args.rules} is not a file", file=sys.stderr)
        sys.exit(1)

    try:
        rules = load_rules(args.rules)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"Error loading rules: {e}", file=sys.stderr)
        sys.exit(1)

    # Find all source files
    source_files = find_source_files(args.root_dir)

    # Search all files
    all_matches = []
    for file_path, language in source_files:
        file_matches = search_file(file_path, language, args.root_dir, rules, args.encoding)
        all_matches.extend(file_matches)

    # Sort matches
    sorted_matches = sort_matches(all_matches)

    # If no fix mode, just output matches
    if not args.dry_run and not args.apply_fixes:
        for match in sorted_matches:
            print(json.dumps(match, separators=(',', ':')))
        sys.exit(0)

    # Process fixes
    # Group matches by file for fix application
    matches_by_file: Dict[str, List[Dict[str, Any]]] = {}
    for match in sorted_matches:
        file_path = match['file']
        if file_path not in matches_by_file:
            matches_by_file[file_path] = []
        matches_by_file[file_path].append(match)

    # Build rule lookup
    rule_lookup = {r['id']: r for r in rules}

    # Process fixes for each file
    all_fixes = []  # List of (match, fix_result) tuples

    for rel_file_path, file_matches in matches_by_file.items():
        full_file_path = Path(args.root_dir) / rel_file_path

        try:
            with open(full_file_path, 'r', encoding=args.encoding) as f:
                content = f.read()
        except (UnicodeDecodeError, OSError):
            continue

        # Collect all potential fixes for this file
        file_fixes = []

        for match in file_matches:
            rule_id = match['rule_id']
            rule = rule_lookup.get(rule_id)

            if rule and 'fix' in rule:
                fix_info = rule['fix']
                template = fix_info['template']

                # Get captures if present
                captures = match.get('captures')
                match_text = match['match']

                # Expand template
                replacement = expand_template(template, match_text, captures)

                # Calculate byte positions
                start_byte = byte_position_from_line_col(content, match['start']['line'], match['start']['col'])
                end_byte = byte_position_from_line_col(content, match['end']['line'], match['end']['col'])

                file_fixes.append({
                    'rule_id': rule_id,
                    'file': rel_file_path,
                    'language': match['language'],
                    'start': match['start'],
                    'end': match['end'],
                    'start_byte': start_byte,
                    'end_byte': end_byte,
                    'replacement': replacement,
                    'match': match
                })

        # Sort fixes by start position, then rule_id
        file_fixes.sort(key=lambda f: (
            f['start']['line'],
            f['start']['col'],
            f['rule_id']
        ))

        # Apply fixes with overlap detection
        applied_ranges = []  # List of (start_byte, end_byte) tuples

        for fix in file_fixes:
            start_byte = fix['start_byte']
            end_byte = fix['end_byte']

            # Check for overlap with already applied fixes
            overlaps = False
            for applied_start, applied_end in applied_ranges:
                if ranges_overlap(start_byte, end_byte, applied_start, applied_end):
                    overlaps = True
                    break

            fix_result = {
                'event': 'fix',
                'rule_id': fix['rule_id'],
                'file': fix['file'],
                'language': fix['language'],
                'start': fix['start'],
                'end': fix['end'],
                'replacement': fix['replacement'],
                'applied': False,
                'skipped_reason': 'overlap' if overlaps else None
            }

            if not overlaps:
                if args.apply_fixes:
                    # Mark for application
                    applied_ranges.append((start_byte, end_byte))
                    fix_result['applied'] = True
                    all_fixes.append((fix['match'], fix_result))
                else:
                    # Dry run - just preview
                    all_fixes.append((fix['match'], fix_result))
            else:
                # Skipped due to overlap
                all_fixes.append((fix['match'], fix_result))

        # Apply fixes to file if needed
        if args.apply_fixes and applied_ranges:
            # Build new content by applying fixes in reverse order (to preserve byte positions)
            content_bytes = bytearray(content.encode('utf-8'))

            # Sort by start_byte descending
            fixes_to_apply = [f for f in file_fixes
                            if any(ranges_overlap(f['start_byte'], f['end_byte'], s, e)
                                  for s, e in applied_ranges)]

            # Actually, we need to track which fixes were applied
            applied_fix_info = []
            for fix in file_fixes:
                start_byte = fix['start_byte']
                end_byte = fix['end_byte']
                for applied_start, applied_end in applied_ranges:
                    if start_byte == applied_start and end_byte == applied_end:
                        applied_fix_info.append(fix)
                        break

            # Sort in reverse order by start_byte
            applied_fix_info.sort(key=lambda f: f['start_byte'], reverse=True)

            # Apply fixes
            for fix in applied_fix_info:
                start_byte = fix['start_byte']
                end_byte = fix['end_byte']
                replacement_bytes = fix['replacement'].encode('utf-8')
                content_bytes = content_bytes[:start_byte] + replacement_bytes + content_bytes[end_byte:]

            # Write back
            with open(full_file_path, 'wb') as f:
                f.write(content_bytes)

    # Build combined output and sort
    combined_output = []

    # Add all matches
    for match in sorted_matches:
        combined_output.append(('match', match))

    # Add all fixes
    for match, fix_result in all_fixes:
        combined_output.append(('fix', fix_result))

    # Sort combined output
    def combined_sort_key(item):
        kind, obj = item
        if kind == 'match':
            return (
                obj['file'],
                obj['start']['line'],
                obj['start']['col'],
                0,  # Match comes before fix
                obj['rule_id']
            )
        else:  # fix
            return (
                obj['file'],
                obj['start']['line'],
                obj['start']['col'],
                1,  # Fix comes after match
                obj['rule_id']
            )

    combined_output.sort(key=combined_sort_key)

    # Output
    for kind, obj in combined_output:
        print(json.dumps(obj, separators=(',', ':')))

    sys.exit(0)


def byte_position_from_line_col(content: str, line: int, col: int) -> int:
    """Convert 1-based line and column to byte position."""
    current_line = 1
    current_col = 1
    byte_pos = 0

    for i, c in enumerate(content):
        if current_line == line and current_col == col:
            return i
        if c == '\n':
            current_line += 1
            current_col = 1
        else:
            current_col += 1

    # If we reach here, return the end position
    return len(content)


if __name__ == '__main__':
    main()
