#!/usr/bin/env python3
"""Command-line code searcher for Python codebases."""

import argparse
import json
import os
import re
from pathlib import Path
from typing import List, Dict, Any

_REGEX_FLAGS = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}


def _make_match(rule_id: str, file_path: str, content: str,
                start: int, end: int, matched_text: str) -> Dict[str, Any]:
    start_line, start_col = _line_col(content, start)
    end_line, end_col = _line_col(content, end)
    return {
        "rule_id": rule_id,
        "file": file_path,
        "language": "python",
        "start": {"line": start_line, "col": start_col},
        "end": {"line": end_line, "col": end_col},
        "match": matched_text
    }


def _line_col(content: str, pos: int) -> tuple:
    """Convert a byte-offset position to 1-based (line, col)."""
    lines = content[:pos].split('\n')
    return len(lines), len(lines[-1]) + 1


def _compile_regex(rule: Dict[str, Any]) -> re.Pattern:
    flags = 0
    for flag in rule.get("regex_flags", []):
        flags |= _REGEX_FLAGS.get(flag, 0)
    return re.compile(rule["pattern"], flags)


def _find_exact(content: str, pattern: str, rule_id: str, file_path: str) -> List[Dict[str, Any]]:
    matches = []
    start = 0
    while True:
        pos = content.find(pattern, start)
        if pos == -1:
            break
        matches.append(_make_match(rule_id, file_path, content, pos, pos + len(pattern), pattern))
        start = pos + 1
    return matches


def _find_regex(content: str, compiled: re.Pattern, rule_id: str, file_path: str) -> List[Dict[str, Any]]:
    return [
        _make_match(rule_id, file_path, content, m.start(), m.end(), m.group())
        for m in compiled.finditer(content)
    ]


_FINDERS = {
    "exact": lambda content, rule, rid, fpath: _find_exact(content, rule["pattern"], rid, fpath),
    "regex": lambda content, rule, rid, fpath: _find_regex(content, _compile_regex(rule), rid, fpath),
}


def scan_file(file_path: str, full_path: Path, rules: List[Dict[str, Any]], encoding: str) -> List[Dict[str, Any]]:
    try:
        with open(full_path, "r", encoding=encoding) as f:
            content = f.read()
    except (UnicodeDecodeError, OSError):
        return []

    all_matches = []
    for rule in rules:
        if "python" not in rule.get("languages", ["python"]):
            continue
        finder = _FINDERS.get(rule["kind"])
        if finder:
            all_matches.extend(finder(content, rule, rule["id"], file_path))
    return all_matches


def main():
    parser = argparse.ArgumentParser(description="Code searcher for Python codebases")
    parser.add_argument("root_dir", help="Path to the codebase to scan")
    parser.add_argument("--rules", required=True, help="Path to a JSON array of rules")
    parser.add_argument("--encoding", default="utf-8", help="File encoding (default: utf-8)")
    args = parser.parse_args()

    root_dir = Path(args.root_dir).resolve()
    with open(args.rules, "r", encoding="utf-8") as f:
        rules = json.load(f)

    all_matches = []
    for dirpath, _, filenames in os.walk(root_dir):
        for filename in filenames:
            if filename.endswith(".py"):
                full_path = Path(dirpath) / filename
                rel_path = full_path.relative_to(root_dir).as_posix()
                all_matches.extend(scan_file(rel_path, full_path, rules, args.encoding))

    all_matches.sort(key=lambda m: (m["file"], m["start"]["line"], m["start"]["col"], m["rule_id"]))
    for match in all_matches:
        print(json.dumps(match, separators=(',', ':')))


if __name__ == "__main__":
    main()
