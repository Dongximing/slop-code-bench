#!/usr/bin/env python3
"""
Code searcher for Python codebases.
Searches for patterns defined in rules and outputs matches as JSON Lines.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import List, Dict, Any, Tuple


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description='Search Python codebase for patterns')
    parser.add_argument('root_dir', type=str, help='Path to the codebase to scan')
    parser.add_argument('--rules', type=str, required=True, help='Path to JSON rules file')
    parser.add_argument('--encoding', type=str, default='utf-8', help='File encoding (default: utf-8)')
    return parser.parse_args()


def validate_rule(rule: Dict[str, Any], rule_ids: set) -> None:
    """Validate a single rule object."""
    # Check required fields
    if 'id' not in rule or not isinstance(rule['id'], str) or not rule['id']:
        raise ValueError(f"Rule must have a non-empty string 'id' field")

    if rule['id'] in rule_ids:
        raise ValueError(f"Duplicate rule id: {rule['id']}")

    if 'kind' not in rule or rule['kind'] not in ('exact', 'regex'):
        raise ValueError(f"Rule {rule['id']}: 'kind' must be 'exact' or 'regex'")

    if 'pattern' not in rule or not isinstance(rule['pattern'], str) or not rule['pattern']:
        raise ValueError(f"Rule {rule['id']}: 'pattern' must be a non-empty string")

    # Check optional fields
    languages = rule.get('languages', ['python'])
    if not isinstance(languages, list):
        raise ValueError(f"Rule {rule['id']}: 'languages' must be an array")
    for lang in languages:
        if lang != 'python':
            raise ValueError(f"Rule {rule['id']}: 'languages' may only contain 'python', got '{lang}'")

    # Check regex_flags for regex rules
    if rule['kind'] == 'regex':
        regex_flags = rule.get('regex_flags', [])
        if not isinstance(regex_flags, list):
            raise ValueError(f"Rule {rule['id']}: 'regex_flags' must be an array")
        allowed_flags = {'i', 'm', 's'}
        for flag in regex_flags:
            if flag not in allowed_flags:
                raise ValueError(f"Rule {rule['id']}: Invalid regex flag '{flag}', allowed: i, m, s")

        # Test compile the regex
        flags = get_regex_flags(regex_flags)

        try:
            re.compile(rule['pattern'], flags)
        except re.error as e:
            raise ValueError(f"Rule {rule['id']}: Invalid regex pattern: {e}")


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
            rule['languages'] = ['python']
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


def find_exact_matches(content: str, pattern: str) -> List[Tuple[int, int, int, int, str]]:
    """
    Find all exact matches in content.
    Returns list of (start_line, start_col, end_line, end_col, match_text)
    """
    matches = []
    start = 0
    while True:
        pos = content.find(pattern, start)
        if pos == -1:
            break

        # Calculate line and column (1-based)
        line_start = content.rfind('\n', 0, pos) + 1
        lines_before = content[:pos].count('\n')
        col = pos - line_start + 1

        # Calculate end position
        end_pos = pos + len(pattern)
        line_end_start = content.rfind('\n', 0, end_pos) + 1
        lines_in_match = content[pos:end_pos].count('\n')

        if lines_in_match == 0:
            # Single line match
            end_line = lines_before + 1
            end_col = col + len(pattern)
        else:
            # Multi-line match
            end_line = lines_before + lines_in_match + 1
            end_col = end_pos - line_end_start + 1

        matches.append((lines_before + 1, col, end_line, end_col, pattern))
        start = pos + 1

    return matches


def find_regex_matches(content: str, pattern: str, flags: int) -> List[Tuple[int, int, int, int, str]]:
    """
    Find all regex matches in content.
    Returns list of (start_line, start_col, end_line, end_col, match_text)
    """
    matches = []

    try:
        compiled = re.compile(pattern, flags)
    except re.error:
        return matches

    for match in compiled.finditer(content):
        pos = match.start()
        match_text = match.group()
        end_pos = match.end()

        # Calculate line and column (1-based)
        line_start = content.rfind('\n', 0, pos) + 1
        lines_before = content[:pos].count('\n')
        col = pos - line_start + 1

        # Calculate end position
        line_end_start = content.rfind('\n', 0, end_pos) + 1
        lines_in_match = content[pos:end_pos].count('\n')

        if lines_in_match == 0:
            # Single line match
            end_line = lines_before + 1
            end_col = col + len(match_text)
        else:
            # Multi-line match
            end_line = lines_before + lines_in_match + 1
            end_col = end_pos - line_end_start + 1

        matches.append((lines_before + 1, col, end_line, end_col, match_text))

    return matches


def search_file(file_path: Path, root_dir: Path, rules: List[Dict[str, Any]], encoding: str) -> List[Dict[str, Any]]:
    """
    Search a single file for all rule matches.
    Returns list of match objects.
    """
    matches = []

    try:
        with open(file_path, 'r', encoding=encoding) as f:
            content = f.read()
    except (UnicodeDecodeError, IOError):
        return matches

    # Get relative path with forward slashes
    rel_path = file_path.relative_to(root_dir).as_posix()

    for rule in rules:
        if rule['kind'] == 'exact':
            found = find_exact_matches(content, rule['pattern'])
        else:  # regex
            flags = get_regex_flags(rule.get('regex_flags', []))
            found = find_regex_matches(content, rule['pattern'], flags)

        for start_line, start_col, end_line, end_col, match_text in found:
            match_obj = {
                'rule_id': rule['id'],
                'file': rel_path,
                'language': 'python',
                'start': {'line': start_line, 'col': start_col},
                'end': {'line': end_line, 'col': end_col},
                'match': match_text
            }
            matches.append(match_obj)

    return matches


def search_directory(root_dir: Path, rules: List[Dict[str, Any]], encoding: str) -> List[Dict[str, Any]]:
    """
    Search all Python files in directory tree.
    Returns list of all matches.
    """
    all_matches = []

    # Walk the directory tree
    for dirpath, dirnames, filenames in os.walk(root_dir):
        for filename in filenames:
            if filename.endswith('.py'):
                file_path = Path(dirpath) / filename
                matches = search_file(file_path, root_dir, rules, encoding)
                all_matches.extend(matches)

    return all_matches


def sort_matches(matches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Sort matches by file, then start.line, then start.col, then rule_id.
    """
    return sorted(matches, key=lambda m: (
        m['file'],
        m['start']['line'],
        m['start']['col'],
        m['rule_id']
    ))


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

    # Search all files
    matches = search_directory(root_dir, rules, args.encoding)

    # Sort matches
    sorted_matches = sort_matches(matches)

    # Output JSON Lines
    for match in sorted_matches:
        print(json.dumps(match))

    sys.exit(0)


if __name__ == '__main__':
    main()
