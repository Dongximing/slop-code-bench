#!/usr/bin/env python3

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import List, Dict, Any


def parse_args():
    parser = argparse.ArgumentParser(description='Code searcher for Python codebases')
    parser.add_argument('root_dir', help='Path to the codebase to scan')
    parser.add_argument('--rules', required=True, help='Path to JSON rules file')
    parser.add_argument('--encoding', default='utf-8', help='File encoding (default: utf-8)')
    return parser.parse_args()


def load_rules(rules_path: str) -> List[Dict[str, Any]]:
    with open(rules_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def validate_rules(rules: List[Dict[str, Any]]) -> None:
    seen_ids = set()
    valid_flags = {'i', 'm', 's'}

    for rule in rules:
        if 'id' not in rule or not rule['id']:
            raise ValueError("Rule missing 'id' field or 'id' is empty")
        if 'kind' not in rule:
            raise ValueError(f"Rule '{rule.get('id', 'unknown')}' missing 'kind' field")
        if 'pattern' not in rule or not rule['pattern']:
            raise ValueError(f"Rule '{rule['id']}' missing 'pattern' field or 'pattern' is empty")

        if rule['id'] in seen_ids:
            raise ValueError(f"Duplicate rule id: '{rule['id']}'")
        seen_ids.add(rule['id'])

        if rule['kind'] not in ('exact', 'regex'):
            raise ValueError(f"Rule '{rule['id']}' has invalid kind: '{rule['kind']}'")

        languages = rule.get('languages', ['python'])
        if not isinstance(languages, list):
            raise ValueError(f"Rule '{rule['id']}' languages must be a list")
        for lang in languages:
            if lang != 'python':
                raise ValueError(f"Rule '{rule['id']}' has unsupported language: '{lang}'")

        if rule['kind'] == 'regex':
            regex_flags = rule.get('regex_flags', [])
            if not isinstance(regex_flags, list):
                raise ValueError(f"Rule '{rule['id']}' regex_flags must be a list")
            for flag in regex_flags:
                if flag not in valid_flags:
                    raise ValueError(f"Rule '{rule['id']}' has invalid regex flag: '{flag}'")
            flags = 0
            for flag in regex_flags:
                if flag == 'i':
                    flags |= re.IGNORECASE
                elif flag == 'm':
                    flags |= re.MULTILINE
                elif flag == 's':
                    flags |= re.DOTALL
            try:
                re.compile(rule['pattern'], flags)
            except re.error as e:
                raise ValueError(f"Rule '{rule['id']}' has invalid regex pattern: {e}")


def get_files(root_dir: str) -> List[Path]:
    """Get all .py files under root_dir, sorted lexicographically."""
    files = []
    root = Path(root_dir)
    for path in root.rglob('*.py'):
        if path.is_file():
            files.append(path)
    return sorted(files)


def calculate_position(content: str, idx: int) -> tuple:
    """Calculate line number and column for a position in content (1-based)."""
    line_num = content.count('\n', 0, idx) + 1
    if idx == 0:
        line_start = 0
    else:
        last_newline = content.rfind('\n', 0, idx)
        line_start = last_newline + 1 if last_newline >= 0 else 0
    col = idx - line_start + 1
    return line_num, col


def find_exact_matches(content: str, pattern: str, rule_id: str, file_path: str) -> List[Dict[str, Any]]:
    """Find all exact matches in content."""
    matches = []
    pos = 0

    while True:
        idx = content.find(pattern, pos)
        if idx == -1:
            break

        start_line, start_col = calculate_position(content, idx)
        end_idx = idx + len(pattern)
        end_line, end_col = calculate_position(content, end_idx)

        matches.append({
            'rule_id': rule_id,
            'file': file_path,
            'language': 'python',
            'start': {'line': start_line, 'col': start_col},
            'end': {'line': end_line, 'col': end_col},
            'match': pattern
        })

        pos = idx + 1

    return matches


def find_regex_matches(content: str, pattern: str, flags: int, rule_id: str, file_path: str) -> List[Dict[str, Any]]:
    """Find all regex matches in content."""
    matches = []
    regex = re.compile(pattern, flags)

    for match in regex.finditer(content):
        start_idx = match.start()
        end_idx = match.end()
        match_text = match.group()

        start_line, start_col = calculate_position(content, start_idx)
        end_line, end_col = calculate_position(content, end_idx)

        matches.append({
            'rule_id': rule_id,
            'file': file_path,
            'language': 'python',
            'start': {'line': start_line, 'col': start_col},
            'end': {'line': end_line, 'col': end_col},
            'match': match_text
        })

    return matches


def main():
    args = parse_args()

    rules = load_rules(args.rules)
    validate_rules(rules)

    files = get_files(args.root_dir)
    all_matches = []

    for file_path in files:
        try:
            with open(file_path, 'r', encoding=args.encoding) as f:
                content = f.read()
        except (UnicodeDecodeError, IOError):
            continue

        rel_path = file_path.relative_to(args.root_dir)
        rel_path_str = str(rel_path).replace(os.sep, '/')

        for rule in rules:
            rule_id = rule['id']
            kind = rule['kind']
            pattern = rule['pattern']
            languages = rule.get('languages', ['python'])

            if 'python' not in languages:
                continue

            if kind == 'exact':
                matches = find_exact_matches(content, pattern, rule_id, rel_path_str)
            elif kind == 'regex':
                flags = 0
                for flag in rule.get('regex_flags', []):
                    if flag == 'i':
                        flags |= re.IGNORECASE
                    elif flag == 'm':
                        flags |= re.MULTILINE
                    elif flag == 's':
                        flags |= re.DOTALL
                matches = find_regex_matches(content, pattern, flags, rule_id, rel_path_str)
            else:
                matches = []

            all_matches.extend(matches)

    all_matches.sort(key=lambda m: (m['file'], m['start']['line'], m['start']['col'], m['rule_id']))

    for match in all_matches:
        print(json.dumps(match, separators=(',', ':')))


if __name__ == '__main__':
    main()