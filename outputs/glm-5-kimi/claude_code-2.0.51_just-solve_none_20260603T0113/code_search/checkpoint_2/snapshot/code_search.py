#!/usr/bin/env python3
"""
Command-line code searcher for Python codebases.
Searches for exact matches and regex patterns in Python files.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Search Python codebases for patterns"
    )
    parser.add_argument(
        "root_dir",
        help="Path to the codebase to scan"
    )
    parser.add_argument(
        "--rules",
        required=True,
        help="Path to a JSON array of rules"
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
    seen_ids = set()
    validated_rules = []

    for i, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise ValueError(f"Rule {i} must be an object")

        # Check required fields
        if "id" not in rule:
            raise ValueError(f"Rule {i} missing 'id' field")
        if "kind" not in rule:
            raise ValueError(f"Rule {i} missing 'kind' field")
        if "pattern" not in rule:
            raise ValueError(f"Rule {i} missing 'pattern' field")

        rule_id = rule["id"]
        if not isinstance(rule_id, str) or not rule_id:
            raise ValueError(f"Rule {i} 'id' must be a non-empty string")

        if rule_id in seen_ids:
            raise ValueError(f"Duplicate rule id: {rule_id}")
        seen_ids.add(rule_id)

        kind = rule["kind"]
        if kind not in ("exact", "regex"):
            raise ValueError(f"Rule {i} 'kind' must be 'exact' or 'regex'")

        pattern = rule["pattern"]
        if not isinstance(pattern, str) or not pattern:
            raise ValueError(f"Rule {i} 'pattern' must be a non-empty string")

        # Validate languages if present
        languages = rule.get("languages", ["python"])
        if not isinstance(languages, list):
            raise ValueError(f"Rule {i} 'languages' must be an array")
        for lang in languages:
            if lang != "python":
                raise ValueError(f"Rule {i} 'languages' may only contain 'python'")

        # Validate regex_flags if present
        regex_flags = rule.get("regex_flags", [])
        if kind == "regex":
            if not isinstance(regex_flags, list):
                raise ValueError(f"Rule {i} 'regex_flags' must be an array")
            for flag in regex_flags:
                if flag not in ("i", "m", "s"):
                    raise ValueError(f"Rule {i} 'regex_flags' may only contain 'i', 'm', 's'")

        validated_rules.append({
            "id": rule_id,
            "kind": kind,
            "pattern": pattern,
            "languages": languages,
            "regex_flags": regex_flags if kind == "regex" else []
        })

    return validated_rules


def compile_regex_flags(flag_list: List[str]) -> int:
    """Convert flag list to re module flags."""
    flags = 0
    for flag in flag_list:
        if flag == "i":
            flags |= re.IGNORECASE
        elif flag == "m":
            flags |= re.MULTILINE
        elif flag == "s":
            flags |= re.DOTALL
    return flags


def find_python_files(root_dir: str) -> List[Path]:
    """Find all .py files under root_dir."""
    root = Path(root_dir).resolve()
    py_files = []
    for path in root.rglob("*.py"):
        if path.is_file():
            py_files.append(path)
    return sorted(py_files)


def get_line_col(content: str, pos: int) -> Tuple[int, int]:
    """
    Convert a position in content to (line, col) 1-based coordinates.
    Line and column are both 1-based.
    """
    line = 1
    col = 1
    for i in range(pos):
        if content[i] == '\n':
            line += 1
            col = 1
        else:
            col += 1
    return line, col


def find_exact_matches(content: str, pattern: str, rule_id: str, file_path: str) -> List[Dict[str, Any]]:
    """Find all exact matches of pattern in content."""
    matches = []
    start = 0
    pattern_len = len(pattern)

    while True:
        pos = content.find(pattern, start)
        if pos == -1:
            break

        start_line, start_col = get_line_col(content, pos)
        end_line, end_col = get_line_col(content, pos + pattern_len)

        matches.append({
            "rule_id": rule_id,
            "file": file_path,
            "language": "python",
            "start": {"line": start_line, "col": start_col},
            "end": {"line": end_line, "col": end_col},
            "match": pattern
        })

        start = pos + 1

    return matches


def find_regex_matches(content: str, pattern: str, flags: int, rule_id: str, file_path: str) -> List[Dict[str, Any]]:
    """Find all regex matches in content."""
    matches = []

    try:
        compiled = re.compile(pattern, flags)
    except re.error:
        return matches

    for match in compiled.finditer(content):
        start_pos = match.start()
        end_pos = match.end()
        matched_text = match.group()

        start_line, start_col = get_line_col(content, start_pos)
        end_line, end_col = get_line_col(content, end_pos)

        matches.append({
            "rule_id": rule_id,
            "file": file_path,
            "language": "python",
            "start": {"line": start_line, "col": start_col},
            "end": {"line": end_line, "col": end_col},
            "match": matched_text
        })

    return matches


def process_file(file_path: Path, root_dir: Path, rules: List[Dict[str, Any]], encoding: str) -> List[Dict[str, Any]]:
    """Process a single file and return all matches."""
    try:
        with open(file_path, 'r', encoding=encoding) as f:
            content = f.read()
    except (UnicodeDecodeError, IOError):
        # Skip files that fail to decode
        return []

    # Get relative path with forward slashes
    rel_path = file_path.relative_to(root_dir)
    file_str = str(rel_path).replace(os.sep, '/')

    all_matches = []

    for rule in rules:
        if rule["kind"] == "exact":
            matches = find_exact_matches(content, rule["pattern"], rule["id"], file_str)
        else:  # regex
            flags = compile_regex_flags(rule["regex_flags"])
            matches = find_regex_matches(content, rule["pattern"], flags, rule["id"], file_str)

        all_matches.extend(matches)

    return all_matches


def sort_key(match: Dict[str, Any]) -> Tuple[str, int, int, str]:
    """Create sort key for a match."""
    return (
        match["file"],
        match["start"]["line"],
        match["start"]["col"],
        match["rule_id"]
    )


def main() -> int:
    """Main entry point."""
    args = parse_args()

    # Load rules
    try:
        rules = load_rules(args.rules)
    except (json.JSONDecodeError, ValueError, IOError) as e:
        print(f"Error loading rules: {e}", file=sys.stderr)
        return 1

    # Find Python files
    root_dir = Path(args.root_dir).resolve()
    if not root_dir.is_dir():
        print(f"Error: {args.root_dir} is not a directory", file=sys.stderr)
        return 1

    py_files = find_python_files(args.root_dir)

    # Process all files
    all_matches = []
    for py_file in py_files:
        matches = process_file(py_file, root_dir, rules, args.encoding)
        all_matches.extend(matches)

    # Sort matches
    all_matches.sort(key=sort_key)

    # Output matches as JSON Lines
    for match in all_matches:
        print(json.dumps(match))

    return 0


if __name__ == "__main__":
    sys.exit(main())