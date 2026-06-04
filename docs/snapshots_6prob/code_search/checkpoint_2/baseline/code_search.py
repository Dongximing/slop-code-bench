#!/usr/bin/env python3
"""Command-line code searcher for Python codebases."""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Search Python codebase for code patterns."
    )
    parser.add_argument(
        "root_dir",
        type=str,
        help="Path to the codebase to scan."
    )
    parser.add_argument(
        "--rules",
        type=str,
        required=True,
        help="Path to a JSON array of rules."
    )
    parser.add_argument(
        "--encoding",
        type=str,
        default="utf-8",
        help="File encoding (default: utf-8)."
    )
    return parser.parse_args()


def load_rules(rules_path: str) -> list[dict[str, Any]]:
    """Load and validate rules from JSON file."""
    with open(rules_path, "r", encoding="utf-8") as f:
        rules = json.load(f)

    if not isinstance(rules, list):
        raise ValueError("Rules must be a JSON array")

    if len(rules) == 0:
        return []

    # Validate each rule
    seen_ids = set()
    for i, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise ValueError(f"Rule at index {i} must be an object")

        # Validate id
        if "id" not in rule or not isinstance(rule["id"], str) or not rule["id"]:
            raise ValueError(f"Rule at index {i} must have a non-empty string id")

        rule_id = rule["id"]
        if rule_id in seen_ids:
            raise ValueError(f"Duplicate rule id: {rule_id}")
        seen_ids.add(rule_id)

        # Validate kind
        if "kind" not in rule or rule["kind"] not in ("exact", "regex"):
            raise ValueError(f"Rule '{rule_id}' must have kind 'exact' or 'regex'")

        # Validate pattern
        if "pattern" not in rule or not isinstance(rule["pattern"], str) or not rule["pattern"]:
            raise ValueError(f"Rule '{rule_id}' must have a non-empty string pattern")

        # Validate languages
        if "languages" in rule:
            langs = rule["languages"]
            if not isinstance(langs, list):
                raise ValueError(f"Rule '{rule_id}' languages must be an array")
            for lang in langs:
                if lang not in ("python", "javascript", "cpp"):
                    raise ValueError(
                        f"Rule '{rule_id}' languages may only contain 'python', 'javascript', 'cpp', got '{lang}'"
                    )

        # Validate regex_flags (only for regex kind)
        if rule["kind"] == "regex" and "regex_flags" in rule:
            flags = rule["regex_flags"]
            if not isinstance(flags, list):
                raise ValueError(f"Rule '{rule_id}' regex_flags must be an array")
            valid_flags = {"i", "m", "s"}
            for flag in flags:
                if flag not in valid_flags:
                    raise ValueError(
                        f"Rule '{rule_id}' has invalid regex flag '{flag}'. "
                        f"Valid flags: i, m, s"
                    )

    return rules


EXTENSION_TO_LANGUAGE = {
    # Python
    ".py": "python",
    # JavaScript
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    # C++
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hh": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
}


def compile_regex(pattern: str, flags_list: list[str] | None) -> re.Pattern:
    """Compile a regex pattern with the given flags."""
    flags = 0
    if flags_list:
        for flag in flags_list:
            if flag == "i":
                flags |= re.IGNORECASE
            elif flag == "m":
                flags |= re.MULTILINE
            elif flag == "s":
                flags |= re.DOTALL

    return re.compile(pattern, flags)


def find_matches_in_line(
    line: str,
    line_num: int,
    rule: dict[str, Any]
) -> list[dict[str, Any]]:
    """Find all matches of a rule in a single line."""
    matches = []
    pattern = rule["pattern"]
    kind = rule["kind"]

    if kind == "exact":
        # Find all non-overlapping exact matches
        start = 0
        while True:
            pos = line.find(pattern, start)
            if pos == -1:
                break
            matches.append({
                "start": pos,
                "end": pos + len(pattern),
                "match_text": pattern
            })
            start = pos + 1  # Allow overlapping matches
    else:  # regex
        regex = compile_regex(pattern, rule.get("regex_flags"))
        for m in regex.finditer(line):
            matches.append({
                "start": m.start(),
                "end": m.end(),
                "match_text": m.group()
            })

    # Convert to output format
    results = []
    for match in matches:
        results.append({
            "rule_id": rule["id"],
            "start": {
                "line": line_num + 1,  # 1-based
                "col": match["start"] + 1  # 1-based
            },
            "end": {
                "line": line_num + 1,  # 1-based
                "col": match["end"] + 1  # 1-based, position AFTER match
            },
            "match": match["match_text"]
        })

    return results


def search_file(
    file_path: Path,
    root_dir: Path,
    rules: list[dict[str, Any]],
    encoding: str
) -> list[dict[str, Any]]:
    """Search a single file for all rule matches."""
    all_matches = []

    try:
        with open(file_path, "r", encoding=encoding) as f:
            lines = f.readlines()
    except UnicodeDecodeError:
        # Skip files that fail to decode
        return all_matches

    # Compute relative path with '/' separators
    rel_path = file_path.relative_to(root_dir)
    file_str = rel_path.as_posix()

    # Determine language from file extension
    suffix = file_path.suffix
    language = EXTENSION_TO_LANGUAGE.get(suffix)
    if language is None:
        # Not a supported file type
        return all_matches

    for line_num, line in enumerate(lines):
        for rule in rules:
            # Check if rule applies to this language
            languages = rule.get("languages")
            if languages is not None and language not in languages:
                continue

            matches = find_matches_in_line(line, line_num, rule)
            for match in matches:
                match["file"] = file_str
                match["language"] = language
                all_matches.append(match)

    return all_matches


def search_directory(
    root_dir: Path,
    rules: list[dict[str, Any]],
    encoding: str
) -> list[dict[str, Any]]:
    """Search all Python files in a directory recursively."""
    all_matches = []

    for file_path in root_dir.rglob("*"):
        if file_path.is_file():
            matches = search_file(file_path, root_dir, rules, encoding)
            all_matches.extend(matches)

    # Sort by: file, start.line, start.col, rule_id
    all_matches.sort(key=lambda m: (m["file"], m["start"]["line"], m["start"]["col"], m["rule_id"]))

    return all_matches


def main() -> None:
    """Main entry point."""
    args = parse_args()

    root_dir = Path(args.root_dir).resolve()
    if not root_dir.exists():
        print(f"Error: Directory '{args.root_dir}' does not exist", file=sys.stderr)
        sys.exit(1)

    if not root_dir.is_dir():
        print(f"Error: '{args.root_dir}' is not a directory", file=sys.stderr)
        sys.exit(1)

    rules_path = Path(args.rules)
    if not rules_path.exists():
        print(f"Error: Rules file '{args.rules}' does not exist", file=sys.stderr)
        sys.exit(1)

    try:
        rules = load_rules(str(rules_path))
    except (json.JSONDecodeError, ValueError) as e:
        print(f"Error loading rules: {e}", file=sys.stderr)
        sys.exit(1)

    matches = search_directory(root_dir, rules, args.encoding)

    # Output JSON Lines
    for match in matches:
        # Reorder fields to match expected schema
        output = {
            "rule_id": match["rule_id"],
            "file": match["file"],
            "language": match["language"],
            "start": match["start"],
            "end": match["end"],
            "match": match["match"]
        }
        print(json.dumps(output))


if __name__ == "__main__":
    main()
