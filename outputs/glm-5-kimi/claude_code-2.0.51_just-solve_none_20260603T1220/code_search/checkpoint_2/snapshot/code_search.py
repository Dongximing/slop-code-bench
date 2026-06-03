#!/usr/bin/env python3
"""
Command-line code searcher for Python, JavaScript, and C++ codebases.
Searches for exact matches and regex patterns in source files.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description='Code searcher for Python, JavaScript, and C++ codebases')
    parser.add_argument('root_dir', help='Path to the codebase to scan')
    parser.add_argument('--rules', required=True, help='Path to JSON rules file')
    parser.add_argument('--encoding', default='utf-8', help='File encoding (default: utf-8)')
    return parser.parse_args()


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
        if 'kind' not in rule or rule['kind'] not in ('exact', 'regex'):
            raise ValueError(f"Rule {i} must have 'kind' of 'exact' or 'regex'")

        # Validate pattern
        if 'pattern' not in rule or not isinstance(rule['pattern'], str) or not rule['pattern']:
            raise ValueError(f"Rule {i} must have a non-empty string 'pattern'")

        # Validate languages (optional)
        valid_languages = {'python', 'javascript', 'cpp'}
        languages = rule.get('languages', list(valid_languages))
        if not isinstance(languages, list) or not languages:
            raise ValueError(f"Rule {i} 'languages' must be a non-empty array")
        for lang in languages:
            if lang not in valid_languages:
                raise ValueError(f"Rule {i} 'languages' may only contain 'python', 'javascript', 'cpp', found: {lang}")

        # Validate regex_flags (optional, only for regex)
        regex_flags = rule.get('regex_flags', [])
        if rule['kind'] == 'regex':
            if not isinstance(regex_flags, list):
                raise ValueError(f"Rule {i} 'regex_flags' must be an array")
            for flag in regex_flags:
                if flag not in ('i', 'm', 's'):
                    raise ValueError(f"Rule {i} 'regex_flags' may only contain 'i', 'm', 's', found: {flag}")

        validated_rule = {
            'id': rule['id'],
            'kind': rule['kind'],
            'pattern': rule['pattern'],
            'languages': languages,
            'regex_flags': regex_flags if rule['kind'] == 'regex' else []
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

        validated_rules.append(validated_rule)

    return validated_rules


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


def line_col_from_pos(text: str, pos: int) -> Tuple[int, int]:
    """Convert byte position to 1-based line and column."""
    line = 1
    col = 1
    for i in range(pos):
        if i >= len(text):
            break
        if text[i] == '\n':
            line += 1
            col = 1
        else:
            col += 1
    return line, col


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


def search_file(file_path: Path, language: str, root_dir: str, rules: List[Dict[str, Any]], encoding: str) -> List[Dict[str, Any]]:
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
                start_line, start_col = line_col_from_pos(content, pos)
                end_line, end_col = line_col_from_pos(content, pos + len(rule['pattern']))

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
                start_line, start_col = line_col_from_pos(content, match.start())
                end_line, end_col = line_col_from_pos(content, match.end())

                match_obj = {
                    'rule_id': rule['id'],
                    'file': relative_path,
                    'language': language,
                    'start': {'line': start_line, 'col': start_col},
                    'end': {'line': end_line, 'col': end_col},
                    'match': match.group()
                }
                matches.append(match_obj)

    return matches


def sort_matches(matches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Sort matches by file, start.line, start.col, then rule_id."""
    def sort_key(m):
        return (
            m['file'],
            m['start']['line'],
            m['start']['col'],
            m['rule_id']
        )

    return sorted(matches, key=sort_key)


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

    # Output as JSON Lines
    for match in sorted_matches:
        print(json.dumps(match, separators=(',', ':')))

    sys.exit(0)


if __name__ == '__main__':
    main()
