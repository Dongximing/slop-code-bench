#!/usr/bin/env python3
"""
Command-line code searcher for Python codebases.
Supports exact match and vanilla regex rules.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, List, Dict, Optional


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Search Python codebases using exact match or regex rules."
    )
    parser.add_argument(
        "root_dir",
        type=str,
        help="Path to the codebase to scan"
    )
    parser.add_argument(
        "--rules",
        type=str,
        required=True,
        help="Path to a JSON array of rules"
    )
    parser.add_argument(
        "--encoding",
        type=str,
        default="utf-8",
        help="File encoding (default: utf-8)"
    )
    return parser.parse_args()


def load_rules(rules_file: str) -> List[Dict[str, Any]]:
    """Load and validate rules from a JSON file."""
    with open(rules_file, 'r', encoding='utf-8') as f:
        rules = json.load(f)

    if not isinstance(rules, list):
        raise ValueError("Rules file must contain a JSON array")

    # Validate each rule
    seen_ids = set()
    for i, rule in enumerate(rules):
        # Check required fields
        if 'id' not in rule or not rule['id']:
            raise ValueError(f"Rule {i}: 'id' must be a non-empty string")
        if rule['id'] in seen_ids:
            raise ValueError(f"Rule {i}: Duplicate rule id: {rule['id']}")
        seen_ids.add(rule['id'])

        if 'kind' not in rule or rule['kind'] not in ('exact', 'regex'):
            raise ValueError(f"Rule {i}: 'kind' must be 'exact' or 'regex'")

        if 'pattern' not in rule or not rule['pattern']:
            raise ValueError(f"Rule {i}: 'pattern' must be a non-empty string")

        # Validate languages if present
        if 'languages' in rule:
            langs = rule['languages']
            if not isinstance(langs, list):
                raise ValueError(f"Rule {i}: 'languages' must be an array")
            for lang in langs:
                if lang != "python":
                    raise ValueError(f"Rule {i}: Unknown language '{lang}', only 'python' is supported")

        # Validate regex_flags for regex rules
        if rule['kind'] == 'regex' and 'regex_flags' in rule:
            flags = rule['regex_flags']
            if not isinstance(flags, list):
                raise ValueError(f"Rule {i}: 'regex_flags' must be an array")
            valid_flags = {'i', 'm', 's'}
            for flag in flags:
                if flag not in valid_flags:
                    raise ValueError(f"Rule {i}: Invalid regex flag '{flag}', must be one of {valid_flags}")

    return rules


def compile_regex_pattern(pattern: str, flags: List[str]) -> re.Pattern:
    """Compile a regex pattern with the given flags."""
    re_flags = 0
    for flag in flags:
        if flag == 'i':
            re_flags |= re.IGNORECASE
        elif flag == 'm':
            re_flags |= re.MULTILINE
        elif flag == 's':
            re_flags |= re.DOTALL

    try:
        return re.compile(pattern, re_flags)
    except re.error as e:
        raise ValueError(f"Invalid regex pattern: {e}")


def find_matches_in_content(
    content: str,
    rule: Dict[str, Any],
    filename: str
) -> List[Dict[str, Any]]:
    """Find all matches for a rule in the given content."""
    matches = []
    pattern = rule['pattern']
    kind = rule['kind']
    rule_id = rule['id']

    if kind == 'exact':
        # For exact match, find all occurrences
        search_str = pattern
        start = 0
        while True:
            idx = content.find(search_str, start)
            if idx == -1:
                break
            # Calculate line and column
            line_num, col_num = get_line_col(content, idx)
            matches.append({
                'rule_id': rule_id,
                'file': filename,
                'language': 'python',
                'start': {'line': line_num, 'col': col_num},
                'end': {'line': get_line_col(content, idx + len(search_str))[0],
                        'col': get_line_col(content, idx + len(search_str))[1]},
                'match': search_str
            })
            start = idx + 1
    else:  # kind == 'regex'
        regex_flags = rule.get('regex_flags', [])
        compiled = compile_regex_pattern(pattern, regex_flags)

        for m in compiled.finditer(content):
            start_pos = m.start()
            end_pos = m.end()
            line_num, col_num = get_line_col(content, start_pos)
            end_line, end_col = get_line_col(content, end_pos)
            matches.append({
                'rule_id': rule_id,
                'file': filename,
                'language': 'python',
                'start': {'line': line_num, 'col': col_num},
                'end': {'line': end_line, 'col': end_col},
                'match': m.group(0)
            })

    return matches


def get_line_col(content: str, pos: int) -> tuple:
    """Get 1-based line and column numbers for a position in content."""
    # Count newlines before this position
    lines_before = content.count('\n', 0, pos)
    # Find the last newline before this position
    last_newline = content.rfind('\n', 0, pos)
    if last_newline == -1:
        col = pos + 1  # 1-based column
    else:
        col = pos - last_newline  # column from start of line
    return (lines_before + 1, col)


def scan_file(
    filepath: Path,
    root_dir: Path,
    rules: List[Dict[str, Any]],
    encoding: str
) -> List[Dict[str, Any]]:
    """Scan a single Python file and return all matches."""
    all_matches = []

    # Read file content
    try:
        with open(filepath, 'r', encoding=encoding) as f:
            content = f.read()
    except UnicodeDecodeError:
        # Skip files that fail to decode
        return all_matches

    # Get relative path with forward slashes
    rel_path = filepath.relative_to(root_dir)
    filename = rel_path.as_posix()

    # Check language support
    for rule in rules:
        languages = rule.get('languages', ['python'])
        if 'python' not in languages:
            continue

        matches = find_matches_in_content(content, rule, filename)
        all_matches.extend(matches)

    return all_matches


def scan_directory(
    root_dir: Path,
    rules: List[Dict[str, Any]],
    encoding: str
) -> List[Dict[str, Any]]:
    """Scan all Python files in a directory and return all matches."""
    all_matches = []

    for py_file in root_dir.rglob('*.py'):
        matches = scan_file(py_file, root_dir, rules, encoding)
        all_matches.extend(matches)

    # Sort matches: by file (lexicographically), then start.line, then start.col, then rule_id
    all_matches.sort(key=lambda m: (m['file'], m['start']['line'], m['start']['col'], m['rule_id']))

    return all_matches


def main() -> int:
    """Main entry point."""
    args = parse_arguments()

    # Validate root directory
    root_dir = Path(args.root_dir)
    if not root_dir.exists():
        print(f"Error: Directory does not exist: {args.root_dir}", file=sys.stderr)
        return 1
    if not root_dir.is_dir():
        print(f"Error: Not a directory: {args.root_dir}", file=sys.stderr)
        return 1

    # Load rules
    try:
        rules = load_rules(args.rules)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"Error loading rules: {e}", file=sys.stderr)
        return 1

    # Scan directory
    matches = scan_directory(root_dir, rules, args.encoding)

    # Output JSON Lines
    for match in matches:
        print(json.dumps(match))

    return 0


if __name__ == '__main__':
    sys.exit(main())
