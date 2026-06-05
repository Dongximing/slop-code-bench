#!/usr/bin/env python3
"""
Command-line code searcher for Python codebases.
Searches for matches based on rules (exact match, regex, or pattern) and outputs JSON Lines.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import List, Dict, Any, Set, Optional, Tuple

import tree_sitter_python
import tree_sitter_javascript
import tree_sitter_cpp
from tree_sitter import Language, Parser


# Language mappings
LANGUAGE_MAP = {
    'python': Language(tree_sitter_python.language()),
    'javascript': Language(tree_sitter_javascript.language()),
    'cpp': Language(tree_sitter_cpp.language()),
}

FILE_EXTENSION_MAP = {
    '.py': 'python',
    '.js': 'javascript',
    '.cpp': 'cpp',
    '.cxx': 'cpp',
    '.cc': 'cpp',
    '.c': 'cpp',
    '.h': 'cpp',
    '.hpp': 'cpp',
    '.hxx': 'cpp',
}

# Cache for parsers
PARSER_CACHE: Dict[str, Parser] = {}


def get_parser(language: str) -> Parser:
    """Get or create a parser for the given language."""
    if language not in PARSER_CACHE:
        parser = Parser(LANGUAGE_MAP[language])
        PARSER_CACHE[language] = parser
    return PARSER_CACHE[language]


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Search codebase for pattern matches"
    )
    parser.add_argument(
        "root_dir",
        help="Path to the codebase to scan"
    )
    parser.add_argument(
        "--rules",
        required=True,
        help="Path to JSON file containing rules array"
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

    # Validate and normalize rules
    seen_ids: Set[str] = set()
    validated_rules = []
    valid_languages = {'python', 'javascript', 'cpp'}
    valid_kinds = {'exact', 'regex', 'pattern'}

    for i, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise ValueError(f"Rule at index {i} must be an object")

        # Validate id
        if 'id' not in rule or not isinstance(rule['id'], str) or not rule['id']:
            raise ValueError(f"Rule at index {i} must have a non-empty string 'id'")
        if rule['id'] in seen_ids:
            raise ValueError(f"Duplicate rule id: {rule['id']}")
        seen_ids.add(rule['id'])

        # Validate kind
        if 'kind' not in rule or rule['kind'] not in valid_kinds:
            raise ValueError(f"Rule at index {i} must have kind 'exact', 'regex', or 'pattern'")

        # Validate pattern
        if 'pattern' not in rule or not isinstance(rule['pattern'], str) or not rule['pattern']:
            raise ValueError(f"Rule at index {i} must have a non-empty string 'pattern'")

        # Validate languages (optional)
        languages = rule.get('languages', ['python', 'javascript', 'cpp'])
        if not isinstance(languages, list):
            raise ValueError(f"Rule {rule['id']}: 'languages' must be an array")
        if not languages:
            languages = ['python', 'javascript', 'cpp']
        for lang in languages:
            if not isinstance(lang, str):
                raise ValueError(f"Rule {rule['id']}: 'languages' must contain strings")
            if lang not in valid_languages:
                raise ValueError(f"Rule {rule['id']}: 'languages' may only contain 'python', 'javascript', 'cpp'")

        # Validate regex_flags (optional, only for regex)
        regex_flags = rule.get('regex_flags', [])
        if rule['kind'] == 'regex':
            if not isinstance(regex_flags, list):
                raise ValueError(f"Rule {rule['id']}: 'regex_flags' must be an array")
            valid_flags = {'i', 'm', 's'}
            for flag in regex_flags:
                if flag not in valid_flags:
                    raise ValueError(f"Rule {rule['id']}: 'regex_flags' may only contain 'i', 'm', 's'")
        elif regex_flags:
            raise ValueError(f"Rule {rule['id']}: 'regex_flags' only valid for regex rules")

        validated_rules.append({
            'id': rule['id'],
            'kind': rule['kind'],
            'pattern': rule['pattern'],
            'languages': languages,
            'regex_flags': regex_flags if rule['kind'] == 'regex' else []
        })

    return validated_rules


def get_source_files(root_dir: str) -> List[Tuple[str, str]]:
    """Get all source files under root_dir with their languages, sorted lexicographically."""
    files = []
    root_path = Path(root_dir)

    for path in root_path.rglob('*'):
        if path.is_file():
            ext = path.suffix.lower()
            if ext in FILE_EXTENSION_MAP:
                rel_path = path.relative_to(root_path)
                files.append((rel_path.as_posix(), FILE_EXTENSION_MAP[ext]))

    return sorted(files, key=lambda x: x[0])


def compile_regex_flags(flags: List[str]) -> int:
    """Convert flag strings to re module flags."""
    flag_map = {'i': re.IGNORECASE, 'm': re.MULTILINE, 's': re.DOTALL}
    result = 0
    for flag in flags:
        result |= flag_map.get(flag, 0)
    return result


def find_exact_matches(content: str, pattern: str, file_path: str, language: str) -> List[Dict[str, Any]]:
    """Find all exact matches in content."""
    matches = []
    lines = content.split('\n')

    for line_num, line in enumerate(lines, start=1):
        col = 1
        while True:
            idx = line.find(pattern, col - 1)
            if idx == -1:
                break

            start_col = idx + 1  # 1-based
            end_col = start_col + len(pattern)

            matches.append({
                'rule_id': None,
                'file': file_path,
                'language': language,
                'start': {'line': line_num, 'col': start_col},
                'end': {'line': line_num, 'col': end_col},
                'match': pattern
            })
            col = start_col + 1

    return matches


def find_regex_matches(content: str, pattern: str, flags: List[str], file_path: str, language: str) -> List[Dict[str, Any]]:
    """Find all regex matches in content with proper line/column tracking."""
    matches = []
    re_flags = compile_regex_flags(flags)

    try:
        compiled = re.compile(pattern, re_flags)
    except re.error as e:
        raise ValueError(f"Invalid regex pattern '{pattern}': {e}")

    lines = content.split('\n')

    line_offsets = [0]
    for line in lines[:-1]:
        line_offsets.append(line_offsets[-1] + len(line) + 1)

    for match in compiled.finditer(content):
        match_text = match.group(0)
        start_pos = match.start()
        end_pos = match.end()

        # Find line number for start position
        start_line = 1
        for i, offset in enumerate(line_offsets):
            if offset <= start_pos:
                start_line = i + 1
            else:
                break

        # Calculate start column
        start_col = start_pos - line_offsets[start_line - 1] + 1

        # Find line number for end position
        end_line = 1
        for i, offset in enumerate(line_offsets):
            if offset < end_pos:
                end_line = i + 1
            else:
                break

        # Calculate end column
        if end_line <= len(line_offsets):
            end_col = end_pos - line_offsets[end_line - 1] + 1
        else:
            # End is at the very end of content
            end_col = len(lines[-1]) + 1 if lines else 1
            end_line = len(lines)

        matches.append({
            'rule_id': None,
            'file': file_path,
            'language': language,
            'start': {'line': start_line, 'col': start_col},
            'end': {'line': end_line, 'col': end_col},
            'match': match_text
        })

    return matches


# ============ Pattern Matching Implementation ============

def extract_metavariables(pattern: str) -> Tuple[str, Dict[str, bool]]:
    """
    Extract metavariables from pattern and replace them with valid identifiers.
    Returns (transformed_pattern, metavar_info) where metavar_info maps name -> is_optional
    """
    metavar_info: Dict[str, bool] = {}

    # Replace $$ with a placeholder first
    placeholder = "__DOLLAR_PLACEHOLDER__"
    pattern = pattern.replace("$$", placeholder)

    # Find all metavariables: $NAME or $NAME?
    # A metavariable starts with $ followed by uppercase letter or underscore,
    # then alphanumeric characters or underscores
    # In Python regex, $ needs to be escaped as \$ in the pattern
    meta_pattern = r'\$([A-Z_][A-Z0-9_]*)(\?)?'

    # Track counter for generating valid identifiers
    counter = [0]

    def replace_meta(match):
        name = match.group(1)
        is_optional = match.group(2) == '?'
        full_name = f"${name}"

        if full_name not in metavar_info:
            metavar_info[full_name] = is_optional
            counter[0] += 1
        elif metavar_info[full_name] != is_optional:
            # Same metavariable with different optional status - use the optional one
            metavar_info[full_name] = True

        # Generate a valid identifier for the language
        return f"__META_{counter[0]}__"

    transformed = re.sub(meta_pattern, replace_meta, pattern)

    # Restore $$ placeholders
    transformed = transformed.replace(placeholder, "$")

    return transformed, metavar_info


def get_text_from_node(source: bytes, node) -> str:
    """Get the text content of a node."""
    return source[node.start_byte:node.end_byte].decode('utf-8')


def get_position(source: bytes, byte_offset: int) -> Tuple[int, int]:
    """Convert byte offset to (line, col) where line and col are 1-based."""
    text = source[:byte_offset].decode('utf-8')
    lines = text.split('\n')
    line = len(lines)
    col = len(lines[-1]) + 1  # 1-based column
    return line, col


def get_node_text_and_range(source: bytes, node) -> Tuple[str, Dict[str, Any]]:
    """Get the text and range dict for a node using tree-sitter's point info."""
    text = get_text_from_node(source, node)
    # tree-sitter provides 0-based row/column via start_point and end_point
    start_point = node.start_point
    end_point = node.end_point
    # Convert to 1-based line and column
    start_line = start_point.row + 1
    start_col = start_point.column + 1
    end_line = end_point.row + 1
    end_col = end_point.column + 1
    return text, {
        'start': {'line': start_line, 'col': start_col},
        'end': {'line': end_line, 'col': end_col}
    }


class PatternMatcher:
    """Matches pattern rules against source code using tree-sitter AST."""

    def __init__(self, pattern: str, language: str):
        self.original_pattern = pattern
        self.language = language
        self.transformed_pattern, self.metavar_info = extract_metavariables(pattern)
        self.parser = get_parser(language)

        # Parse the pattern to get its AST
        pattern_bytes = self.transformed_pattern.encode('utf-8')
        self.pattern_tree = self.parser.parse(pattern_bytes)
        self.pattern_root = self.pattern_tree.root_node

        # Get the significant child from the pattern root
        # (skip wrapper nodes like module/expression_statement if the pattern has only one meaningful child)
        self.pattern_node = self._get_significant_pattern_node(self.pattern_root)

    def _get_significant_pattern_node(self, node):
        """Extract the significant child from a wrapper node like module."""
        # If the node is a module/program/translation_unit with a single child, use that child
        if node.type in ('module', 'program', 'translation_unit') and len(node.children) == 1:
            child = node.children[0]
            # If the child is an expression_statement with a single child, use that
            if child.type == 'expression_statement' and len(child.children) == 1:
                return child.children[0]
            return child
        return node

    def find_matches(self, source: bytes) -> List[Dict[str, Any]]:
        """Find all matches in the source code."""
        tree = self.parser.parse(source)
        root = tree.root_node

        matches = []
        self._match_node(root, source, matches)
        return matches

    def _match_node(self, node, source: bytes, matches: List[Dict[str, Any]]):
        """Recursively try to match pattern against node and its children."""
        bindings: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {}

        if self._try_match(node, self.pattern_node, source, bindings):
            # Found a match - build the match result
            match_text, match_range = get_node_text_and_range(source, node)

            captures = {}
            for meta_name in sorted(bindings.keys()):
                texts_and_ranges = bindings[meta_name]
                # All occurrences must have the same text
                first_text = texts_and_ranges[0][0]
                ranges = [tr[1] for tr in texts_and_ranges]

                captures[meta_name] = {
                    'text': first_text,
                    'ranges': ranges
                }

            matches.append({
                'match': match_text,
                'range': match_range,
                'captures': captures
            })

        # Recurse into children
        for child in node.children:
            self._match_node(child, source, matches)

    def _try_match(self, source_node, pattern_node, source: bytes,
                   bindings: Dict[str, List[Tuple[str, Dict[str, Any]]]]) -> bool:
        """
        Try to match pattern_node against source_node.
        Returns True if match succeeds, False otherwise.
        bindings is updated with metavariable bindings.
        """
        # Check if this is a metavariable placeholder
        pattern_text = get_text_from_node(self.transformed_pattern.encode('utf-8'), pattern_node)

        # Check for metavariable placeholder
        meta_match = re.match(r'^__META_(\d+)__$', pattern_text)
        if meta_match:
            # This is a metavariable
            meta_num = int(meta_match.group(1))
            # Find the original metavariable name
            meta_names = list(self.metavar_info.keys())
            if meta_num > 0 and meta_num <= len(meta_names):
                meta_name = meta_names[meta_num - 1]
            else:
                meta_name = meta_names[meta_num - 1] if meta_num <= len(meta_names) else None

            if meta_name is None:
                return False

            # Get the source text and range
            source_text, source_range = get_node_text_and_range(source, source_node)

            # Check if this metavariable was already bound
            if meta_name in bindings:
                # All occurrences must match the same text
                if bindings[meta_name][0][0] != source_text:
                    return False
                # Add this occurrence
                bindings[meta_name].append((source_text, source_range))
                return True
            else:
                # New binding
                bindings[meta_name] = [(source_text, source_range)]
                return True

        # Not a metavariable - check node types match
        if source_node.type != pattern_node.type:
            return False

        # Check if both have the same number of children
        source_children = list(source_node.children)
        pattern_children = list(pattern_node.children)

        if len(source_children) != len(pattern_children):
            # Try to match if pattern has optional metavariables that could be missing
            # For now, require exact structural match
            return False

        # Recursively match children
        for sc, pc in zip(source_children, pattern_children):
            if not self._try_match(sc, pc, source, bindings):
                return False

        # Check text content for leaf nodes (no children)
        if len(pattern_children) == 0:
            source_text = get_text_from_node(source, source_node)
            pattern_text_raw = get_text_from_node(self.original_pattern.encode('utf-8'), pattern_node)

            # If pattern is a literal (not metavariable), must match exactly
            if not re.match(r'^__META_\d+__$', pattern_text):
                if source_text != pattern_text:
                    return False

        return True


def find_pattern_matches(content: str, pattern: str, language: str, file_path: str) -> List[Dict[str, Any]]:
    """Find all pattern matches in content using tree-sitter."""
    try:
        matcher = PatternMatcher(pattern, language)
        source = content.encode('utf-8')
        raw_matches = matcher.find_matches(source)

        matches = []
        for raw in raw_matches:
            match = {
                'rule_id': None,
                'file': file_path,
                'language': language,
                'start': raw['range']['start'],
                'end': raw['range']['end'],
                'match': raw['match'],
            }
            if raw['captures']:
                match['captures'] = raw['captures']
            matches.append(match)

        return matches
    except Exception as e:
        # If pattern parsing fails, return no matches
        return []


def search_file(file_path: str, full_path: str, rules: List[Dict[str, Any]],
                encoding: str, language: str) -> List[Dict[str, Any]]:
    """Search a single file for all rule matches."""
    try:
        with open(full_path, 'r', encoding=encoding) as f:
            content = f.read()
    except (UnicodeDecodeError, IOError):
        return []

    all_matches = []

    for rule in rules:
        # Skip rules that don't apply to this language
        if language not in rule['languages']:
            continue

        if rule['kind'] == 'exact':
            matches = find_exact_matches(content, rule['pattern'], file_path, language)
        elif rule['kind'] == 'regex':
            matches = find_regex_matches(content, rule['pattern'], rule['regex_flags'], file_path, language)
        else:  # pattern
            matches = find_pattern_matches(content, rule['pattern'], language, file_path)

        for match in matches:
            match['rule_id'] = rule['id']

        all_matches.extend(matches)

    return all_matches


def sort_matches(matches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Sort matches by file, then start position, then end position, then rule_id.
    For same start position: earlier end first, then rule_id.
    """
    return sorted(matches, key=lambda m: (
        m['file'],
        m['start']['line'],
        m['start']['col'],
        m['end']['line'],
        m['end']['col'],
        m['rule_id']
    ))


def main():
    """Main entry point."""
    args = parse_args()

    # Load rules
    try:
        rules = load_rules(args.rules)
    except Exception as e:
        print(f"Error loading rules: {e}", file=sys.stderr)
        sys.exit(1)

    # Get all source files
    try:
        source_files = get_source_files(args.root_dir)
    except Exception as e:
        print(f"Error scanning directory: {e}", file=sys.stderr)
        sys.exit(1)

    # Search each file
    all_matches = []
    for rel_file, language in source_files:
        full_path = os.path.join(args.root_dir, rel_file)
        matches = search_file(rel_file, full_path, rules, args.encoding, language)
        all_matches.extend(matches)

    # Sort matches
    sorted_matches = sort_matches(all_matches)

    # Output JSON Lines to stdout
    for match in sorted_matches:
        # Ensure captures keys are sorted when serializing
        if 'captures' in match:
            match['captures'] = dict(sorted(match['captures'].items()))
        print(json.dumps(match, separators=(',', ': ')))

    sys.exit(0)


if __name__ == '__main__':
    main()
