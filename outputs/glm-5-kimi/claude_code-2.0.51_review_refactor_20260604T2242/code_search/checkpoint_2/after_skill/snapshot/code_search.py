#!/usr/bin/env python3
"""
Command-line code searcher for Python codebases.
Searches for matches based on rules (exact match or regex) and outputs JSON Lines.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import List, Dict, Any, Set


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Search Python codebase for pattern matches"
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
        if 'kind' not in rule or rule['kind'] not in ('exact', 'regex'):
            raise ValueError(f"Rule at index {i} must have kind 'exact' or 'regex'")

        # Validate pattern
        if 'pattern' not in rule or not isinstance(rule['pattern'], str) or not rule['pattern']:
            raise ValueError(f"Rule at index {i} must have a non-empty string 'pattern'")

        # Validate languages (optional)
        languages = rule.get('languages', ['python'])
        if not isinstance(languages, list):
            raise ValueError(f"Rule {rule['id']}: 'languages' must be an array")
        for lang in languages:
            if not isinstance(lang, str):
                raise ValueError(f"Rule {rule['id']}: 'languages' must contain strings")
            if lang != 'python':
                raise ValueError(f"Rule {rule['id']}: 'languages' may only contain 'python'")

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


def get_python_files(root_dir: str) -> List[str]:
    """Get all .py files under root_dir, sorted lexicographically."""
    python_files = []
    root_path = Path(root_dir)

    for path in root_path.rglob('*.py'):
        if path.is_file():
            rel_path = path.relative_to(root_path)
            python_files.append(rel_path.as_posix())

    return sorted(python_files)


def compile_regex_flags(flags: List[str]) -> int:
    """Convert flag strings to re module flags."""
    flag_map = {'i': re.IGNORECASE, 'm': re.MULTILINE, 's': re.DOTALL}
    result = 0
    for flag in flags:
        result |= flag_map.get(flag, 0)
    return result


def find_exact_matches(content: str, pattern: str, file_path: str) -> List[Dict[str, Any]]:
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
                'language': 'python',
                'start': {'line': line_num, 'col': start_col},
                'end': {'line': line_num, 'col': end_col},
                'match': pattern
            })
            col = start_col + 1

    return matches


def find_regex_matches(content: str, pattern: str, flags: List[str], file_path: str) -> List[Dict[str, Any]]:
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
            'language': 'python',
            'start': {'line': start_line, 'col': start_col},
            'end': {'line': end_line, 'col': end_col},
            'match': match_text
        })

    return matches


def search_file(file_path: str, full_path: str, rules: List[Dict[str, Any]], encoding: str) -> List[Dict[str, Any]]:
    """Search a single file for all rule matches."""
    try:
        with open(full_path, 'r', encoding=encoding) as f:
            content = f.read()
    except (UnicodeDecodeError, IOError):
        return []

    all_matches = []

    for rule in rules:
        if rule['kind'] == 'exact':
            matches = find_exact_matches(content, rule['pattern'], file_path)
        else:
            matches = find_regex_matches(content, rule['pattern'], rule['regex_flags'], file_path)

        for match in matches:
            match['rule_id'] = rule['id']

        all_matches.extend(matches)

    return all_matches


def sort_matches(matches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Sort matches by file, then start.line, then start.col, then rule_id."""
    return sorted(matches, key=lambda m: (
        m['file'],
        m['start']['line'],
        m['start']['col'],
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

    # Get all Python files
    try:
        python_files = get_python_files(args.root_dir)
    except Exception as e:
        print(f"Error scanning directory: {e}", file=sys.stderr)
        sys.exit(1)

    # Search each file
    all_matches = []
    for rel_file in python_files:
        full_path = os.path.join(args.root_dir, rel_file)
        matches = search_file(rel_file, full_path, rules, args.encoding)
        all_matches.extend(matches)

    # Sort matches
    sorted_matches = sort_matches(all_matches)

    # Output JSON Lines to stdout
    for match in sorted_matches:
        print(json.dumps(match, separators=(',', ': ')))

    sys.exit(0)


if __name__ == '__main__':
    main()
