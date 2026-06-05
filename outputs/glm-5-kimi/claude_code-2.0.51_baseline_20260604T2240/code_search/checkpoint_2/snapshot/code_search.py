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
from typing import List, Dict, Any, Optional


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Search Python codebases for pattern matches"
    )
    parser.add_argument(
        "root_dir",
        help="Path to the codebase to scan"
    )
    parser.add_argument(
        "--rules",
        required=True,
        help="Path to JSON file containing search rules"
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

        # Validate id
        rule_id = rule.get("id")
        if not rule_id or not isinstance(rule_id, str):
            raise ValueError(f"Rule {i}: 'id' must be a non-empty string")
        if rule_id in seen_ids:
            raise ValueError(f"Rule {i}: duplicate id '{rule_id}'")
        seen_ids.add(rule_id)

        # Validate kind
        kind = rule.get("kind")
        if kind not in ("exact", "regex"):
            raise ValueError(f"Rule {i}: 'kind' must be 'exact' or 'regex'")

        # Validate pattern
        pattern = rule.get("pattern")
        if not pattern or not isinstance(pattern, str):
            raise ValueError(f"Rule {i}: 'pattern' must be a non-empty string")

        # Validate languages (optional)
        languages = rule.get("languages", ["python"])
        if not isinstance(languages, list):
            raise ValueError(f"Rule {i}: 'languages' must be an array")
        for lang in languages:
            if lang != "python":
                raise ValueError(f"Rule {i}: 'languages' may only contain 'python'")

        # Validate regex_flags (optional, only for regex)
        regex_flags = rule.get("regex_flags", [])
        if kind == "regex":
            if not isinstance(regex_flags, list):
                raise ValueError(f"Rule {i}: 'regex_flags' must be an array")
            valid_flags = {"i", "m", "s"}
            for flag in regex_flags:
                if flag not in valid_flags:
                    raise ValueError(f"Rule {i}: invalid regex flag '{flag}'")
        elif regex_flags:
            raise ValueError(f"Rule {i}: 'regex_flags' only valid for regex rules")

        # Compile regex pattern if needed
        compiled_pattern = None
        if kind == "regex":
            flags = 0
            for flag in regex_flags:
                if flag == "i":
                    flags |= re.IGNORECASE
                elif flag == "m":
                    flags |= re.MULTILINE
                elif flag == "s":
                    flags |= re.DOTALL

            try:
                compiled_pattern = re.compile(pattern, flags)
            except re.error as e:
                raise ValueError(f"Rule {i}: invalid regex pattern: {e}")

        validated_rules.append({
            "id": rule_id,
            "kind": kind,
            "pattern": pattern,
            "languages": languages,
            "regex_flags": regex_flags,
            "compiled_pattern": compiled_pattern
        })

    return validated_rules


def find_python_files(root_dir: str) -> List[Path]:
    """Find all Python files recursively under root_dir."""
    python_files = []
    root_path = Path(root_dir)

    for path in root_path.rglob("*.py"):
        if path.is_file():
            python_files.append(path)

    return sorted(python_files)


def find_exact_matches(content: str, pattern: str, rule_id: str, file_path: str) -> List[Dict[str, Any]]:
    """Find all exact matches of pattern in content."""
    matches = []

    # Find all occurrences
    start = 0
    while True:
        pos = content.find(pattern, start)
        if pos == -1:
            break

        # Calculate line and column for start position
        start_line, start_col = get_line_col(content, pos)

        # Calculate line and column for end position
        end_pos = pos + len(pattern)
        end_line, end_col = get_line_col(content, end_pos)

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


def find_regex_matches(content: str, compiled_pattern, rule_id: str, file_path: str) -> List[Dict[str, Any]]:
    """Find all regex matches in content."""
    matches = []

    for match in compiled_pattern.finditer(content):
        start_pos = match.start()
        end_pos = match.end()
        match_text = match.group(0)

        # Calculate line and column for start position
        start_line, start_col = get_line_col(content, start_pos)

        # Calculate line and column for end position
        end_line, end_col = get_line_col(content, end_pos)

        matches.append({
            "rule_id": rule_id,
            "file": file_path,
            "language": "python",
            "start": {"line": start_line, "col": start_col},
            "end": {"line": end_line, "col": end_col},
            "match": match_text
        })

    return matches


def get_line_col(content: str, pos: int) -> tuple:
    """Convert a position in content to (line, col) where both are 1-based."""
    # Count newlines before this position
    line_num = 1
    line_start = 0

    for i in range(pos):
        if content[i] == '\n':
            line_num += 1
            line_start = i + 1

    # Column is position within the line (1-based)
    col_num = pos - line_start + 1

    return line_num, col_num


def process_file(file_path: Path, root_dir: Path, rules: List[Dict], encoding: str) -> List[Dict[str, Any]]:
    """Process a single file and return all matches."""
    try:
        with open(file_path, 'r', encoding=encoding) as f:
            content = f.read()
    except (UnicodeDecodeError, IOError):
        # Skip files that fail to decode
        return []

    # Get relative path with forward slashes
    rel_path = file_path.relative_to(root_dir).as_posix()

    all_matches = []

    for rule in rules:
        if rule["kind"] == "exact":
            matches = find_exact_matches(content, rule["pattern"], rule["id"], rel_path)
        else:  # regex
            matches = find_regex_matches(content, rule["compiled_pattern"], rule["id"], rel_path)

        all_matches.extend(matches)

    return all_matches


def sort_key(match: Dict[str, Any]) -> tuple:
    """Generate sort key for a match."""
    return (
        match["file"],
        match["start"]["line"],
        match["start"]["col"],
        match["rule_id"]
    )


def main():
    """Main entry point."""
    args = parse_args()

    # Load rules
    try:
        rules = load_rules(args.rules)
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as e:
        print(f"Error loading rules: {e}", file=sys.stderr)
        sys.exit(1)

    # Find Python files
    root_path = Path(args.root_dir)
    if not root_path.is_dir():
        print(f"Error: {args.root_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    python_files = find_python_files(args.root_dir)

    # Process all files
    all_matches = []
    for file_path in python_files:
        matches = process_file(file_path, root_path, rules, args.encoding)
        all_matches.extend(matches)

    # Sort matches
    all_matches.sort(key=sort_key)

    # Output JSON Lines
    for match in all_matches:
        print(json.dumps(match, separators=(',', ':')))

    sys.exit(0)


if __name__ == "__main__":
    main()