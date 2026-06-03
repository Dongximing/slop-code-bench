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
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class Rule:
    id: str
    kind: str  # "exact" or "regex"
    pattern: str
    languages: List[str] = field(default_factory=list)
    regex_flags: List[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.languages:
            self.languages = ["python"]


@dataclass
class Position:
    line: int
    col: int


@dataclass
class Match:
    rule_id: str
    file: str
    language: str
    start: Position
    end: Position
    match: str


def parse_args():
    parser = argparse.ArgumentParser(
        description="Search codebase for patterns using rules"
    )
    parser.add_argument("root_dir", help="Path to the codebase to scan")
    parser.add_argument("--rules", required=True, help="Path to JSON rules file")
    parser.add_argument("--encoding", default="utf-8", help="File encoding (default: utf-8)")
    return parser.parse_args()


def load_rules(rules_path: str) -> List[Rule]:
    """Load and validate rules from JSON file."""
    with open(rules_path, "r", encoding="utf-8") as f:
        rules_data = json.load(f)

    rules = []
    rule_ids = set()

    for idx, rule_data in enumerate(rules_data):
        # Validate required fields
        if "id" not in rule_data:
            raise ValueError(f"Rule at index {idx} missing required field 'id'")
        if "kind" not in rule_data:
            raise ValueError(f"Rule at index {idx} missing required field 'kind'")
        if "pattern" not in rule_data:
            raise ValueError(f"Rule at index {idx} missing required field 'pattern'")

        rule_id = rule_data["id"]
        if not rule_id:
            raise ValueError(f"Rule at index {idx} has empty 'id'")
        if rule_id in rule_ids:
            raise ValueError(f"Duplicate rule id: {rule_id}")
        rule_ids.add(rule_id)

        kind = rule_data["kind"]
        if kind not in ("exact", "regex"):
            raise ValueError(f"Rule {rule_id}: invalid kind '{kind}', must be 'exact' or 'regex'")

        pattern = rule_data["pattern"]
        if not pattern:
            raise ValueError(f"Rule {rule_id}: pattern cannot be empty")

        languages = rule_data.get("languages", [])
        if languages:
            if not isinstance(languages, list):
                raise ValueError(f"Rule {rule_id}: 'languages' must be an array")
            for lang in languages:
                if lang != "python":
                    raise ValueError(f"Rule {rule_id}: unsupported language '{lang}', only 'python' is supported")

        regex_flags = rule_data.get("regex_flags", [])
        if regex_flags:
            if kind != "regex":
                raise ValueError(f"Rule {rule_id}: 'regex_flags' is only valid for kind='regex'")
            if not isinstance(regex_flags, list):
                raise ValueError(f"Rule {rule_id}: 'regex_flags' must be an array")
            valid_flags = {"i", "m", "s"}
            for flag in regex_flags:
                if flag not in valid_flags:
                    raise ValueError(f"Rule {rule_id}: invalid regex flag '{flag}', must be one of 'i', 'm', 's'")

            # Compile pattern with flags to validate
            flags = 0
            if "i" in regex_flags:
                flags |= re.IGNORECASE
            if "m" in regex_flags:
                flags |= re.MULTILINE
            if "s" in regex_flags:
                flags |= re.DOTALL
            try:
                re.compile(pattern, flags)
            except re.error as e:
                raise ValueError(f"Rule {rule_id}: invalid regex pattern: {e}")
        elif kind == "regex":
            # Validate the pattern compiles even without flags
            try:
                re.compile(pattern)
            except re.error as e:
                raise ValueError(f"Rule {rule_id}: invalid regex pattern: {e}")

        rule = Rule(
            id=rule_id,
            kind=kind,
            pattern=pattern,
            languages=languages,
            regex_flags=regex_flags
        )
        rules.append(rule)

    return rules


def get_python_files(root_dir: str) -> List[Path]:
    """Recursively get all .py files in root_dir."""
    root_path = Path(root_dir)
    py_files = []

    for file_path in root_path.rglob("*"):
        if file_path.is_file() and file_path.suffix == ".py":
            py_files.append(file_path)

    return py_files


def calculate_position(text: str, position: int) -> Position:
    """Calculate 1-based line and column from character position."""
    line_count = text.count("\n", 0, position)
    line = line_count + 1

    # Find the last newline before position to calculate column
    last_newline = text.rfind("\n", 0, position)
    col = position - last_newline  # column is characters since last newline

    return Position(line=line, col=col)


def find_exact_matches(rule: Rule, content: str, file_path: Path, root_dir: Path) -> List[Match]:
    """Find all exact matches of rule.pattern in content."""
    matches = []
    pattern = rule.pattern

    start_idx = 0
    while True:
        idx = content.find(pattern, start_idx)
        if idx == -1:
            break

        end_idx = idx + len(pattern)
        start_pos = calculate_position(content, idx)
        end_pos = calculate_position(content, end_idx)

        # Get relative path with '/' separators
        rel_path = file_path.relative_to(root_dir)
        file_str = str(rel_path).replace(os.sep, "/")

        match = Match(
            rule_id=rule.id,
            file=file_str,
            language="python",
            start=start_pos,
            end=end_pos,
            match=pattern
        )
        matches.append(match)

        start_idx = end_idx

    return matches


def find_regex_matches(rule: Rule, content: str, file_path: Path, root_dir: Path) -> List[Match]:
    """Find all regex matches of rule.pattern in content."""
    matches = []

    flags = 0
    if "i" in rule.regex_flags:
        flags |= re.IGNORECASE
    if "m" in rule.regex_flags:
        flags |= re.MULTILINE
    if "s" in rule.regex_flags:
        flags |= re.DOTALL

    try:
        compiled_pattern = re.compile(rule.pattern, flags)
    except re.error:
        return matches  # Should have been validated already

    for m in compiled_pattern.finditer(content):
        start_idx = m.start()
        end_idx = m.end()
        matched_text = m.group()

        start_pos = calculate_position(content, start_idx)
        end_pos = calculate_position(content, end_idx)

        # Get relative path with '/' separators
        rel_path = file_path.relative_to(root_dir)
        file_str = str(rel_path).replace(os.sep, "/")

        match = Match(
            rule_id=rule.id,
            file=file_str,
            language="python",
            start=start_pos,
            end=end_pos,
            match=matched_text
        )
        matches.append(match)

    return matches


def scan_file(file_path: Path, rules: List[Rule], root_dir: Path, encoding: str) -> List[Match]:
    """Scan a single file for all rule matches."""
    matches = []

    try:
        with open(file_path, "r", encoding=encoding) as f:
            content = f.read()
    except UnicodeDecodeError:
        # Skip files that fail to decode
        return matches

    for rule in rules:
        if rule.kind == "exact":
            file_matches = find_exact_matches(rule, content, file_path, root_dir)
        else:  # regex
            file_matches = find_regex_matches(rule, content, file_path, root_dir)
        matches.extend(file_matches)

    return matches


def matches_to_json(match: Match) -> dict:
    """Convert a Match dataclass to JSON-serializable dict."""
    return {
        "rule_id": match.rule_id,
        "file": match.file,
        "language": match.language,
        "start": {"line": match.start.line, "col": match.start.col},
        "end": {"line": match.end.line, "col": match.end.col},
        "match": match.match
    }


def main():
    args = parse_args()

    # Validate root_dir
    if not os.path.isdir(args.root_dir):
        print(f"Error: '{args.root_dir}' is not a directory", file=sys.stderr)
        sys.exit(1)

    # Load rules
    try:
        rules = load_rules(args.rules)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"Error loading rules: {e}", file=sys.stderr)
        sys.exit(1)

    # Get all Python files
    root_path = Path(args.root_dir)
    py_files = get_python_files(args.root_dir)

    # Scan all files
    all_matches = []
    for file_path in py_files:
        file_matches = scan_file(file_path, rules, root_path, args.encoding)
        all_matches.extend(file_matches)

    # Sort matches by file, then start.line, then start.col, then rule_id
    all_matches.sort(key=lambda m: (m.file, m.start.line, m.start.col, m.rule_id))

    # Output JSON Lines
    for match in all_matches:
        print(json.dumps(matches_to_json(match)))

    # Exit code 0 on success (even if zero matches)
    sys.exit(0)


if __name__ == "__main__":
    main()
