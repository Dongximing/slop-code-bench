#!/usr/bin/env python3
"""Command-line code searcher for Python codebases."""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import List, Dict, Any, Optional


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Code searcher for Python codebases")
    parser.add_argument("root_dir", help="Path to the codebase to scan")
    parser.add_argument("--rules", required=True, help="Path to a JSON array of rules")
    parser.add_argument("--encoding", default="utf-8", help="File encoding (default: utf-8)")
    return parser.parse_args()


def load_rules(rules_path: str) -> List[Dict[str, Any]]:
    """Load rules from a JSON file."""
    with open(rules_path, "r", encoding="utf-8") as f:
        rules = json.load(f)
    return rules


def compile_regex(rule: Dict[str, Any]) -> re.Pattern:
    """Compile a regex pattern from a rule."""
    pattern = rule["pattern"]
    flags = 0
    regex_flags = rule.get("regex_flags", [])
    for flag in regex_flags:
        if flag == "i":
            flags |= re.IGNORECASE
        elif flag == "m":
            flags |= re.MULTILINE
        elif flag == "s":
            flags |= re.DOTALL
    return re.compile(pattern, flags)


def get_line_col(content: str, pos: int) -> tuple:
    """Convert a position in content to (line, col) 1-based."""
    lines = content[:pos].split('\n')
    line = len(lines)
    col = len(lines[-1]) + 1
    return line, col


def find_exact_matches(content: str, pattern: str, rule_id: str, file_path: str) -> List[Dict[str, Any]]:
    """Find all exact matches of pattern in content."""
    matches = []
    start = 0
    while True:
        pos = content.find(pattern, start)
        if pos == -1:
            break
        start_line, start_col = get_line_col(content, pos)
        end_line, end_col = get_line_col(content, pos + len(pattern))
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


def find_regex_matches(content: str, compiled: re.Pattern, rule_id: str, file_path: str) -> List[Dict[str, Any]]:
    """Find all regex matches in content."""
    matches = []
    for m in compiled.finditer(content):
        start_line, start_col = get_line_col(content, m.start())
        end_line, end_col = get_line_col(content, m.end())
        matches.append({
            "rule_id": rule_id,
            "file": file_path,
            "language": "python",
            "start": {"line": start_line, "col": start_col},
            "end": {"line": end_line, "col": end_col},
            "match": m.group()
        })
    return matches


def scan_file(file_path: str, full_path: Path, rules: List[Dict[str, Any]], encoding: str) -> List[Dict[str, Any]]:
    """Scan a single file for all rule matches."""
    try:
        with open(full_path, "r", encoding=encoding) as f:
            content = f.read()
    except (UnicodeDecodeError, OSError):
        return []

    all_matches = []
    for rule in rules:
        rule_id = rule["id"]
        kind = rule["kind"]
        languages = rule.get("languages", ["python"])

        # Check if this rule applies to Python
        if "python" not in languages:
            continue

        if kind == "exact":
            matches = find_exact_matches(content, rule["pattern"], rule_id, file_path)
        elif kind == "regex":
            compiled = compile_regex(rule)
            matches = find_regex_matches(content, compiled, rule_id, file_path)
        else:
            continue

        all_matches.extend(matches)

    return all_matches


def main():
    args = parse_args()

    root_dir = Path(args.root_dir).resolve()
    rules = load_rules(args.rules)
    encoding = args.encoding

    all_matches = []

    # Walk the directory tree and find all .py files
    for dirpath, dirnames, filenames in os.walk(root_dir):
        for filename in filenames:
            if filename.endswith(".py"):
                full_path = Path(dirpath) / filename
                rel_path = full_path.relative_to(root_dir)
                # Use POSIX-style path with forward slashes
                file_path = rel_path.as_posix()
                matches = scan_file(file_path, full_path, rules, encoding)
                all_matches.extend(matches)

    # Sort matches by file, start.line, start.col, rule_id
    all_matches.sort(key=lambda m: (m["file"], m["start"]["line"], m["start"]["col"], m["rule_id"]))

    # Output JSON Lines
    for match in all_matches:
        print(json.dumps(match, separators=(',', ':')))

    sys.exit(0)


if __name__ == "__main__":
    main()
